import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

from organizer.item_processor import BoundaryPolicy, ItemProcessor, PlanRequest


@dataclass(frozen=True)
class AttemptSummary:
    attempt_id: str
    watch_id: str
    source_path: str
    source_fingerprint: str
    rule_name: str
    status: str
    failure_detail: str
    created_at: str
    retry_of_attempt_id: str | None = None


@dataclass(frozen=True)
class AttemptReviewDetails:
    attempt_id: str
    watch_id: str
    source_path: str
    source_fingerprint: str
    source_size: int
    source_mtime: float
    rule_name: str
    status: str
    planned_actions: tuple[dict[str, object], ...]
    action_results: tuple[dict[str, object], ...]
    resulting_paths: tuple[str, ...]
    failure_detail: str
    suppressions: tuple[dict[str, object], ...]
    linked_attempts: tuple[str, ...]
    processing_lineage: tuple[str, ...]
    accepted_results: tuple[dict[str, object], ...]
    abandoned_reason: str
    created_at: str
    completed_at: str


@dataclass(frozen=True)
class AttemptFilters:
    statuses: tuple[str, ...] = ()
    watch_id: str = ""


@dataclass(frozen=True)
class CommandResult:
    success: bool
    attempt_id: str
    status: str
    detail: str = ""
    new_attempt_id: str | None = None


@dataclass(frozen=True)
class Accept:
    action_index: int
    resulting_path: str
    watch_root: Path
    boundary_policy: BoundaryPolicy | None = None


@dataclass(frozen=True)
class Abandon:
    reason: str


@dataclass(frozen=True)
class Reopen:
    watch_root: Path
    rules_path: Path
    boundary_policy: BoundaryPolicy | None = None


@dataclass(frozen=True)
class RetryFromStart:
    watch_root: Path
    rules_path: Path
    boundary_policy: BoundaryPolicy | None = None


AttemptReviewCommand = Accept | Abandon | Reopen | RetryFromStart


class AttemptReview:
    def __init__(self, processor: ItemProcessor) -> None:
        self._processor = processor
        self._attempts_path = processor._attempts_path

    def list(self, filters: AttemptFilters | None = None) -> list[AttemptSummary]:
        filters = filters or AttemptFilters()
        query = "SELECT attempt_id, watch_id, source_path, source_fingerprint, rule_name, status, failure_detail, started_at, retry_of_attempt_id FROM processing_attempts WHERE 1=1"
        params: list[object] = []
        if filters.statuses:
            placeholders = ", ".join("?" for _ in filters.statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(filters.statuses)
        if filters.watch_id:
            query += " AND watch_id = ?"
            params.append(filters.watch_id)
        query += " ORDER BY started_at DESC"
        with sqlite3.connect(self._attempts_path) as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            AttemptSummary(
                attempt_id=row[0],
                watch_id=row[1],
                source_path=row[2],
                source_fingerprint=row[3],
                rule_name=row[4],
                status=row[5],
                failure_detail=row[6] or "",
                created_at=row[7] or "",
                retry_of_attempt_id=row[8] if row[8] else None,
            )
            for row in rows
        ]

    def inspect(self, attempt_id: str) -> AttemptReviewDetails:
        with sqlite3.connect(self._attempts_path) as connection:
            row = connection.execute(
                "SELECT attempt_id, watch_id, source_path, source_fingerprint, source_size, source_mtime, rule_name, status, planned_actions, action_results, resulting_paths, failure_detail, processing_lineage, accepted_results, abandoned_reason, started_at, completed_at, retry_of_attempt_id FROM processing_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"attempt not found: {attempt_id}")
        (
            db_attempt_id, watch_id, source_path, source_fingerprint, source_size, source_mtime,
            rule_name, status, planned_actions_json, action_results_json, resulting_paths_json,
            failure_detail, lineage_json, accepted_json, abandoned_reason, started_at, completed_at,
            retry_of_attempt_id,
        ) = row
        suppressions = self._suppressions_for_attempt(attempt_id)
        linked = self._linked_attempts(attempt_id, retry_of_attempt_id)
        return AttemptReviewDetails(
            attempt_id=db_attempt_id,
            watch_id=watch_id,
            source_path=source_path,
            source_fingerprint=source_fingerprint or "",
            source_size=source_size or 0,
            source_mtime=source_mtime or 0.0,
            rule_name=rule_name or "",
            status=status,
            planned_actions=tuple(json.loads(planned_actions_json or "[]")),
            action_results=tuple(json.loads(action_results_json or "[]")),
            resulting_paths=tuple(json.loads(resulting_paths_json or "[]")),
            failure_detail=failure_detail or "",
            suppressions=tuple(suppressions),
            linked_attempts=tuple(linked),
            processing_lineage=tuple(json.loads(lineage_json or "[]")),
            accepted_results=tuple(json.loads(accepted_json or "[]")),
            abandoned_reason=abandoned_reason or "",
            created_at=started_at or "",
            completed_at=completed_at or "",
        )

    def command(self, attempt_id: str, command: AttemptReviewCommand) -> CommandResult:
        if isinstance(command, Accept):
            return self._accept(attempt_id, command)
        if isinstance(command, Abandon):
            return self._abandon(attempt_id, command)
        if isinstance(command, Reopen):
            return self._reopen(attempt_id, command)
        if isinstance(command, RetryFromStart):
            return self._retry_from_start(attempt_id, command)
        raise ValueError(f"unknown command: {command}")

    def _accept(self, attempt_id: str, command: Accept) -> CommandResult:
        details = self.inspect(attempt_id)
        if details.status != "needs-reconciliation":
            raise ValueError(f"attempt {attempt_id} is not in needs-reconciliation: {details.status}")
        if command.action_index < 0 or command.action_index >= len(details.action_results):
            raise ValueError(f"action index {command.action_index} out of range")
        for existing in details.accepted_results:
            if existing.get("action_index") == command.action_index:
                raise ValueError(f"action {command.action_index} already accepted")
        planned_actions = list(details.planned_actions)
        action_results = list(details.action_results)
        planned_action = planned_actions[command.action_index]
        action_result = action_results[command.action_index]
        planned_target = self._processor._canonical_path(Path(str(planned_action.get("target", ""))))
        submitted_path = self._processor._canonical_path(Path(command.resulting_path))
        if submitted_path != planned_target:
            raise ValueError(f"unexpected resulting path: {command.resulting_path}")
        policy = command.boundary_policy or BoundaryPolicy()
        self._validate_accepted_path_boundaries(submitted_path, policy)
        is_delete = str(planned_action.get("kind", "")) == "delete"
        if is_delete:
            if submitted_path.exists():
                raise ValueError(f"delete result path must not exist: {command.resulting_path}")
            fingerprint = ""
        else:
            if not submitted_path.exists():
                raise ValueError(f"resulting path does not exist: {command.resulting_path}")
            if submitted_path.is_symlink():
                raise ValueError(f"resulting path is a symlink: {command.resulting_path}")
            fingerprint = self._processor._fingerprint(submitted_path)
            if str(planned_action.get("kind", "")) in ("move", "copy", "rename"):
                source_path_str = str(action_result.get("source") or "")
                if source_path_str:
                    source_path = self._processor._canonical_path(Path(source_path_str))
                    if source_path.exists() and not source_path.is_symlink():
                        expected_fingerprint = self._processor._fingerprint(source_path)
                    else:
                        expected_fingerprint = details.source_fingerprint if command.action_index == 0 else ""
                else:
                    expected_fingerprint = details.source_fingerprint if command.action_index == 0 else ""
                if expected_fingerprint and fingerprint != expected_fingerprint:
                    raise ValueError("fingerprint mismatch: resulting path does not match expected identity")
        accepted_entry: dict[str, object] = {
            "action_index": command.action_index,
            "resulting_path": command.resulting_path,
            "accepted_at": str(time.time()),
            "fingerprint": fingerprint,
        }
        new_accepted = list(details.accepted_results) + [accepted_entry]
        with sqlite3.connect(self._attempts_path) as connection:
            connection.execute(
                "UPDATE processing_attempts SET accepted_results = ? WHERE attempt_id = ?",
                (json.dumps(new_accepted), attempt_id),
            )
        return CommandResult(
            success=True,
            attempt_id=attempt_id,
            status=details.status,
            detail=f"accepted action {command.action_index} resulting path: {command.resulting_path}",
        )

    def _validate_accepted_path_boundaries(self, path: Path, policy: BoundaryPolicy) -> None:
        if policy.config_root and self._processor._is_within(path, self._processor._canonical_path(policy.config_root)):
            raise ValueError("accepted path is within the config volume")
        if policy.data_roots and not any(
            self._processor._is_within(path, self._processor._canonical_path(root))
            for root in policy.data_roots
        ):
            raise ValueError("accepted path is outside data volumes")
        if policy.allowed_destinations and not any(
            self._processor._is_within(path, self._processor._canonical_path(root))
            for root in policy.allowed_destinations
        ):
            raise ValueError("accepted path is outside allowed destination roots")

    def _abandon(self, attempt_id: str, command: Abandon) -> CommandResult:
        details = self.inspect(attempt_id)
        if details.status == "completed":
            raise ValueError(f"attempt {attempt_id} cannot be abandoned: {details.status}")
        if details.status == "abandoned":
            raise ValueError(f"attempt {attempt_id} is already abandoned")
        with sqlite3.connect(self._attempts_path) as connection:
            connection.execute(
                "UPDATE processing_attempts SET status = ?, abandoned_reason = ?, completed_at = ? WHERE attempt_id = ?",
                ("abandoned", command.reason, str(time.time()), attempt_id),
            )
        source = Path(details.source_path)
        self._processor._create_suppression(
            details.watch_id, source, details.source_fingerprint, attempt_id, "abandoned",
        )
        return CommandResult(
            success=True,
            attempt_id=attempt_id,
            status="abandoned",
            detail=f"abandoned: {command.reason}",
        )

    def _reopen(self, attempt_id: str, command: Reopen) -> CommandResult:
        details = self.inspect(attempt_id)
        if details.status != "abandoned":
            raise ValueError(f"attempt {attempt_id} is not abandoned: {details.status}")
        source = self._processor._canonical_path(Path(details.source_path))
        request = PlanRequest(
            watch_id=details.watch_id,
            watch_root=self._processor._canonical_path(command.watch_root),
            item=source,
            rules_path=command.rules_path,
            boundary_policy=command.boundary_policy,
        )
        plan = self._processor.plan(request)
        report = self._processor.execute(plan, retry_of_attempt_id=attempt_id)
        if report.status == "completed":
            self._processor.clear_suppression(details.watch_id, source, details.source_fingerprint)
        with sqlite3.connect(self._attempts_path) as connection:
            row = connection.execute(
                "SELECT attempt_id FROM processing_attempts WHERE retry_of_attempt_id = ? ORDER BY started_at DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        new_attempt_id = row[0] if row else ""
        return CommandResult(
            success=True,
            attempt_id=attempt_id,
            status=details.status,
            detail="reopened: fresh plan created",
            new_attempt_id=new_attempt_id,
        )

    def _retry_from_start(self, attempt_id: str, command: RetryFromStart) -> CommandResult:
        details = self.inspect(attempt_id)
        if details.status not in ("failed", "needs-reconciliation"):
            raise ValueError(f"attempt {attempt_id} is not retryable: {details.status}")
        source = self._processor._canonical_path(Path(details.source_path))
        request = PlanRequest(
            watch_id=details.watch_id,
            watch_root=self._processor._canonical_path(command.watch_root),
            item=source,
            rules_path=command.rules_path,
            boundary_policy=command.boundary_policy,
        )
        plan = self._processor.plan(request)
        report = self._processor.execute(plan, retry_of_attempt_id=attempt_id)
        if report.status == "completed":
            self._processor.clear_suppression(details.watch_id, source, details.source_fingerprint)
        with sqlite3.connect(self._attempts_path) as connection:
            row = connection.execute(
                "SELECT attempt_id FROM processing_attempts WHERE retry_of_attempt_id = ? ORDER BY started_at DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        new_attempt_id = row[0] if row else ""
        return CommandResult(
            success=True,
            attempt_id=attempt_id,
            status=details.status,
            detail=f"retry from start: new attempt {new_attempt_id}",
            new_attempt_id=new_attempt_id,
        )

    def _suppressions_for_attempt(self, attempt_id: str) -> List[dict[str, object]]:
        with sqlite3.connect(self._attempts_path) as connection:
            rows = connection.execute(
                "SELECT watch_id, source_path, source_fingerprint, suppressed_at, reason FROM processing_suppressions WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchall()
        return [
            {
                "watch_id": row[0],
                "source_path": row[1],
                "source_fingerprint": row[2],
                "suppressed_at": row[3],
                "reason": row[4],
            }
            for row in rows
        ]

    def _linked_attempts(
        self,
        attempt_id: str,
        retry_of_attempt_id: str | None,
    ) -> List[str]:
        linked: List[str] = []
        with sqlite3.connect(self._attempts_path) as connection:
            rows = connection.execute(
                "SELECT attempt_id FROM processing_attempts WHERE retry_of_attempt_id = ? ORDER BY started_at",
                (attempt_id,),
            ).fetchall()
            linked.extend(row[0] for row in rows)
        if retry_of_attempt_id:
            linked.append(retry_of_attempt_id)
        return linked
