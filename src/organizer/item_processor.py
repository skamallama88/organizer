from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class ExecutionMode(StrEnum):
    APPLY = "apply"
    DRY_RUN = "dry-run"


@dataclass(frozen=True)
class BoundaryPolicy:
    """Mounted path policy used to validate watch and action boundaries."""

    data_roots: tuple[Path, ...] = ()
    config_root: Path | None = None
    watch_roots: tuple[Path, ...] = ()
    allowed_destinations: tuple[Path, ...] = ()
    case_sensitive: bool | None = None


@dataclass(frozen=True)
class PlanRequest:
    watch_id: str
    watch_root: Path
    item: Path
    rules_path: Path
    boundary_policy: BoundaryPolicy | None = None


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    target: Path


@dataclass(frozen=True)
class Plan:
    watch_id: str
    source: Path
    source_size: int
    source_mtime: float
    rule_name: str
    actions: tuple[PlannedAction, ...]
    diagnostics: tuple[str, ...]
    source_fingerprint: str = ""
    ruleset_revision: str = ""
    rules_path: Path | None = None


@dataclass(frozen=True)
class ActionResult:
    kind: str
    target: Path
    result: str
    detail: str = ""
    source: Path | None = None
    resulting_path: Path | None = None


@dataclass(frozen=True)
class ExecutionReport:
    status: str
    dry_run: bool
    actions: tuple[ActionResult, ...]


class ItemProcessor:
    """Plans and executes item actions without exposing storage details to callers."""

    def __init__(self, attempts_path: Path, events: list[dict[str, str]] | None = None) -> None:
        self._attempts_path = attempts_path
        self._events = events if events is not None else []
        self._initialize_attempts()

    def plan(self, request: PlanRequest) -> Plan:
        policy = request.boundary_policy or BoundaryPolicy()
        item = self._canonical_path(request.item)
        watch_root = self._canonical_path(request.watch_root)
        diagnostics: list[str] = []
        self._validate_policy(policy, watch_root, item)
        matching_rule: tuple[str, list[dict[str, Any]], dict[str, re.Match[str]]] | None = None
        ruleset_revision = self._ruleset_revision(request.rules_path)

        loaded = yaml.safe_load(request.rules_path.read_text()) or {}
        rules = loaded.get("rules", []) if isinstance(loaded, dict) else []
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")

        invalid_earlier: list[str] = []
        for index, rule in enumerate(rules):
            try:
                name, conditions, actions = self._validate_rule(rule)
                candidate_matches = {
                    condition_name: re.search(pattern, self._match_value(field, item))
                    for condition_name, (field, pattern) in conditions.items()
                }
                if all(match is not None for match in candidate_matches.values()):
                    typed_matches: dict[str, re.Match[str]] = {
                        condition_name: match
                        for condition_name, match in candidate_matches.items()
                        if match is not None
                    }
                    self._validate_action_references(actions, typed_matches)
                    matching_rule = (name, actions, typed_matches)
                    break
            except (TypeError, ValueError, re.error) as error:
                invalid_earlier.append(f"rule {index + 1} invalid: {error}")

        if matching_rule is None:
            raise ValueError("no valid rule matched item")

        diagnostics.extend(invalid_earlier)
        if invalid_earlier and len(diagnostics) == 1:
            diagnostics[0] = f"disabled earlier rule 1: {diagnostics[0]}"

        rule_name, action_specs, matches = matching_rule
        planned: list[PlannedAction] = []
        current = item
        for action in action_specs:
            if set(action) == {"rename"} and isinstance(action["rename"], dict):
                name_value = action["rename"].get("name")
                name = name_value if isinstance(name_value, str) else ""
                if not isinstance(name, str) or not name:
                    raise ValueError(f"rule {rule_name} rename name is required")
                target_name = self._expand_captures(name, matches)
                if not target_name or target_name in {".", ".."} or Path(target_name).name != target_name:
                    raise ValueError(f"rule {rule_name} rename name is invalid")
                target = current.parent / target_name
                self._validate_destination_item(target)
                if target != current and (target.exists() or self._case_collision(target, policy)):
                    raise ValueError(f"destination collision: {target}")
                planned.append(PlannedAction(kind="rename", target=target))
                current = target
                continue
            kind = next(iter(action), "")
            if kind == "delete":
                if action is not action_specs[-1]:
                    raise ValueError(f"rule {rule_name} delete result cannot accept a later action")
                planned.append(PlannedAction(kind="delete", target=current))
                continue
            if kind not in {"move", "copy"} or not isinstance(action.get(kind), dict):
                raise ValueError(f"rule {rule_name} has unsupported action")
            destination = action[kind].get("destination")
            if not isinstance(destination, str) or not destination:
                raise ValueError(f"rule {rule_name} move destination is required")
            root = Path(destination)
            if not root.is_absolute():
                root = watch_root / root
            destination_root = self._resolve_destination(root)
            self._validate_destination(policy, watch_root, current, destination_root)
            current = destination_root / current.name
            self._validate_destination_item(current)
            if current.exists() or self._case_collision(current, policy):
                raise ValueError(f"destination collision: {current}")
            if destination_root in {self._canonical_path(root) for root in policy.watch_roots}:
                diagnostics.append(f"destination is another watch folder: {destination_root}")
            planned.append(PlannedAction(kind=kind, target=current))

        stat = item.stat()
        return Plan(
            watch_id=request.watch_id,
            source=item,
            source_size=stat.st_size,
            source_mtime=stat.st_mtime,
            source_fingerprint=self._fingerprint(item),
            rule_name=rule_name,
            actions=tuple(planned),
            diagnostics=tuple(diagnostics),
            ruleset_revision=ruleset_revision,
            rules_path=request.rules_path,
        )

    def execute(self, plan: Plan, mode: ExecutionMode = ExecutionMode.APPLY) -> ExecutionReport:
        if mode is ExecutionMode.DRY_RUN:
            self._validate_source(plan)
            if plan.rules_path is not None and self._ruleset_revision(plan.rules_path) != plan.ruleset_revision:
                raise ValueError("stale plan: ruleset revision changed")
            dry_run_results = tuple(
                ActionResult(action.kind, action.target, "DRY_RUN", "would execute", plan.source, action.target)
                for action in plan.actions
            )
            for result in dry_run_results:
                self._emit(plan, result)
            return ExecutionReport(status="dry-run", dry_run=True, actions=dry_run_results)

        self._validate_source(plan)
        if plan.rules_path is not None and self._ruleset_revision(plan.rules_path) != plan.ruleset_revision:
            raise ValueError("stale plan: ruleset revision changed")
        attempt_id = str(uuid.uuid4())
        self._start_attempt(attempt_id, plan)
        results: list[ActionResult] = []
        source = plan.source
        try:
            for action in plan.actions:
                if action.kind == "delete":
                    if source.is_dir():
                        shutil.rmtree(source)
                    else:
                        source.unlink()
                    result = ActionResult(action.kind, action.target, "OK", source=source)
                elif action.kind == "copy":
                    staging = self._copy_to_staging(source, action.target)
                    try:
                        self._validate_source(plan)
                    except ValueError as error:
                        staging.unlink(missing_ok=True)
                        raise OSError(str(error)) from error
                    self._publish_staged(staging, action.target)
                    result = ActionResult(action.kind, action.target, "OK", source=source, resulting_path=action.target)
                else:
                    action_source = source
                    self._validate_destination_item(action.target)
                    if action.target != source:
                        if self._case_collision(action.target, BoundaryPolicy()) or action.target.exists():
                            raise FileExistsError(f"destination already exists: {action.target}")
                        action.target.parent.mkdir(parents=True, exist_ok=True)
                        source.rename(action.target)
                    source = action.target
                    result = ActionResult(action.kind, action.target, "OK", source=action_source, resulting_path=source)
                results.append(result)
                self._emit(plan, result)
        except OSError as error:
            result = ActionResult(plan.actions[len(results)].kind, plan.actions[len(results)].target, "FAILED", str(error), source=source)
            results.append(result)
            self._finish_attempt(attempt_id, "failed", results)
            self._emit(plan, result)
            return ExecutionReport(status="failed", dry_run=False, actions=tuple(results))

        self._finish_attempt(attempt_id, "completed", results)
        return ExecutionReport(status="completed", dry_run=False, actions=tuple(results))

    def attempts(self) -> list[dict[str, object]]:
        with sqlite3.connect(self._attempts_path) as connection:
            rows = connection.execute(
                "SELECT status, resulting_paths, copy_provenance FROM processing_attempts ORDER BY rowid"
            ).fetchall()
        return [{"status": status, "resulting_paths": json.loads(paths), **({"copy_provenance": json.loads(provenance)} if provenance else {})} for status, paths, provenance in rows]

    def _initialize_attempts(self) -> None:
        self._attempts_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._attempts_path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS processing_attempts (
                attempt_id TEXT PRIMARY KEY,
                watch_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                status TEXT NOT NULL,
                resulting_paths TEXT NOT NULL,
                copy_provenance TEXT,
                action_results TEXT NOT NULL DEFAULT '[]'
                )"""
            )

    def _start_attempt(self, attempt_id: str, plan: Plan) -> None:
        with sqlite3.connect(self._attempts_path) as connection:
            connection.execute(
                "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, copy_provenance) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, plan.watch_id, str(plan.source), plan.rule_name, "started", "[]", None),
            )

    def _finish_attempt(self, attempt_id: str, status: str, results: list[ActionResult]) -> None:
        paths = [str(result.resulting_path or result.target) for result in results if result.result == "OK"]
        provenance = next((json.dumps({"source": str(result.source), "result": str(result.resulting_path)}) for result in results if result.kind == "copy" and result.result == "OK"), None)
        action_results = json.dumps([{"kind": result.kind, "target": str(result.target), "result": result.result, "detail": result.detail, "source": str(result.source) if result.source else None, "resulting_path": str(result.resulting_path) if result.resulting_path else None} for result in results])
        with sqlite3.connect(self._attempts_path) as connection:
            connection.execute(
                "UPDATE processing_attempts SET status = ?, resulting_paths = ?, copy_provenance = ?, action_results = ? WHERE attempt_id = ?",
                (status, json.dumps(paths), provenance, action_results, attempt_id),
            )

    def _emit(self, plan: Plan, result: ActionResult) -> None:
        self._events.append(
            {
                "watch": plan.watch_id,
                "rule": plan.rule_name,
                "action": result.kind,
                "item": str(plan.source),
                "result": result.result,
                "detail": result.detail,
            }
        )

    def _copy_to_staging(self, source: Path, target: Path) -> Path:
        staging = target.parent / f".organizer-staging-{uuid.uuid4()}"
        if source.is_dir():
            shutil.copytree(source, staging)
        else:
            staging.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staging)
        return staging

    @staticmethod
    def _publish_staged(staging: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if staging.is_dir():
                shutil.rmtree(staging)
            else:
                staging.unlink(missing_ok=True)
            raise FileExistsError(f"destination already exists: {target}")
        staging.rename(target)

    @staticmethod
    def _validate_rule(rule: object) -> tuple[str, dict[str, tuple[str, str]], list[dict[str, Any]]]:
        if not isinstance(rule, dict):
            raise ValueError("rule must be a mapping")
        name = rule.get("name")
        match = rule.get("match")
        conditions = rule.get("conditions")
        actions = rule.get("actions")
        if not isinstance(name, str) or not name:
            raise ValueError("name is required")
        if not isinstance(match, dict):
            raise ValueError("match is required")
        if conditions is None:
            conditions = {"match": match}
        if not isinstance(conditions, dict) or not conditions:
            raise ValueError("conditions are required")
        parsed_conditions: dict[str, tuple[str, str]] = {}
        for condition_name, condition in conditions.items():
            if not isinstance(condition_name, str) or not condition_name or not isinstance(condition, dict):
                raise ValueError("condition name and mapping are required")
            field = condition.get("field")
            pattern = condition.get("pattern")
            if field not in {"file_name", "folder_name", "full_path"}:
                raise ValueError("match field is invalid")
            if not isinstance(pattern, str):
                raise ValueError("match pattern is required")
            re.compile(pattern)
            parsed_conditions[condition_name] = (field, pattern)
        if not isinstance(actions, list) or not actions:
            raise ValueError("actions are required")
        return name, parsed_conditions, actions

    @staticmethod
    def _ruleset_revision(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _validate_action_references(actions: list[dict[str, Any]], matches: dict[str, re.Match[str]]) -> None:
        for action in actions:
            if set(action) == {"rename"} and isinstance(action["rename"], dict):
                name = action["rename"].get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("rename name is required")
                try:
                    for reference in re.findall(r"\\(?:[1-9][0-9]*|g<[^>]+>)", name):
                        condition_name, capture = ItemProcessor._split_capture_reference(reference)
                        matches[condition_name].expand(capture)
                except (IndexError, re.error, ValueError) as error:
                    raise ValueError(f"invalid capture reference: {error}") from error

    @staticmethod
    def _expand_captures(value: str, matches: dict[str, re.Match[str]]) -> str:
        def replace(reference: re.Match[str]) -> str:
            condition_name, capture = ItemProcessor._split_capture_reference(reference.group(0))
            return matches[condition_name].expand(capture)

        return re.sub(r"(?:(?P<condition>[A-Za-z_][A-Za-z0-9_]*)\.)?\\(?:[1-9][0-9]*|g<[^>]+>)", replace, value)

    @staticmethod
    def _split_capture_reference(reference: str) -> tuple[str, str]:
        if "." in reference:
            condition_name, capture = reference.split(".", 1)
            return condition_name, capture
        return "match", reference

    @staticmethod
    def _match_value(field: str, item: Path) -> str:
        if field == "full_path":
            return str(Path(os.path.normpath(str(item))))
        if field == "folder_name":
            return item.name if item.is_dir() else item.parent.name
        return item.name

    @staticmethod
    def _validate_source(plan: Plan) -> None:
        stat = plan.source.stat()
        if stat.st_size != plan.source_size or stat.st_mtime != plan.source_mtime or ItemProcessor._fingerprint(plan.source) != plan.source_fingerprint:
            raise ValueError("stale plan: source changed")

    @classmethod
    def _fingerprint(cls, path: Path) -> str:
        digest = hashlib.sha256()
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_symlink():
                    digest.update(f"link:{child.relative_to(path)}:{os.readlink(child)}".encode())
                elif child.is_file():
                    digest.update(f"file:{child.relative_to(path)}:".encode())
                    with child.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
        else:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _canonical_path(path: Path) -> Path:
        return Path(os.path.abspath(os.path.normpath(path))).resolve(strict=False)

    @classmethod
    def _resolve_destination(cls, path: Path) -> Path:
        current = Path(os.path.abspath(os.path.normpath(path)))
        cursor = Path(current.anchor)
        for component in current.parts[1:]:
            cursor /= component
            if cursor.is_symlink():
                raise ValueError(f"unsafe symlink traversal in destination: {path}")
        return current.resolve(strict=False)

    @classmethod
    def _validate_policy(cls, policy: BoundaryPolicy, watch_root: Path, item: Path) -> None:
        data_roots = tuple(cls._canonical_path(root) for root in policy.data_roots)
        config_root = cls._canonical_path(policy.config_root) if policy.config_root else None
        watch_roots = tuple(cls._canonical_path(root) for root in policy.watch_roots)
        for destination in policy.allowed_destinations:
            cls._validate_destination_root(policy, cls._canonical_path(destination))
        if config_root and cls._is_within(watch_root, config_root):
            raise ValueError("config volume cannot be a watch root")
        if data_roots and not any(cls._is_within(watch_root, root) for root in data_roots):
            raise ValueError("watch root must be within a data volume")
        if watch_roots and watch_root not in watch_roots:
            watch_roots = (*watch_roots, watch_root)
        for index, first in enumerate(watch_roots):
            for second in watch_roots[index + 1 :]:
                if cls._is_within(first, second) or cls._is_within(second, first):
                    raise ValueError("watch roots must be disjoint")
        if config_root and cls._is_within(item, config_root):
            raise ValueError("config volume cannot be watched or targeted")

    @classmethod
    def _validate_destination_root(cls, policy: BoundaryPolicy, destination: Path) -> None:
        if policy.config_root and cls._is_within(destination, cls._canonical_path(policy.config_root)):
            raise ValueError("allowed destination cannot be within the config volume")
        if policy.data_roots and not any(
            cls._is_within(destination, cls._canonical_path(root)) for root in policy.data_roots
        ):
            raise ValueError("allowed destination must be within a data volume")
        cls._resolve_destination(destination)

    @classmethod
    def _validate_destination(
        cls, policy: BoundaryPolicy, watch_root: Path, source: Path, destination_root: Path
    ) -> None:
        if policy.config_root and cls._is_within(destination_root, cls._canonical_path(policy.config_root)):
            raise ValueError("destination cannot be within the config volume")
        if policy.data_roots and not any(
            cls._is_within(destination_root, cls._canonical_path(root)) for root in policy.data_roots
        ):
            raise ValueError("destination must be within a data volume")
        if policy.allowed_destinations and not any(
            cls._is_within(destination_root, cls._canonical_path(root))
            for root in policy.allowed_destinations
        ):
            raise ValueError("destination is not an allowed destination root")
        if cls._is_within(destination_root, source) and source.is_dir():
            raise ValueError("self-targeting or descendant-targeting destination")
        if destination_root == source.parent and source.name == destination_root.name:
            raise ValueError("self-targeting destination")
        if destination_root == watch_root and source.name == watch_root.name:
            raise ValueError("self-targeting destination")

    @classmethod
    def _validate_destination_item(cls, path: Path) -> None:
        for parent in (path.parent, *path.parent.parents):
            if parent.is_symlink():
                raise ValueError(f"unsafe symlink traversal in destination: {path}")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @classmethod
    def _case_collision(cls, path: Path, policy: BoundaryPolicy) -> bool:
        if policy.case_sensitive is True:
            return False
        if path.parent.exists():
            return any(candidate.name.casefold() == path.name.casefold() for candidate in path.parent.iterdir())
        return False
