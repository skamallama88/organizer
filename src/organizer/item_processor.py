from __future__ import annotations

import json
import errno
import hashlib
import lzma
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
import py7zr
from py7zr.exceptions import ArchiveError as _Py7zArchiveError
from py7zr.exceptions import PasswordRequired as _Py7zPasswordRequired
import rarfile  # type: ignore[import-untyped]

from organizer.structured_log import LogEntry, LogLevel, LogResult, StructuredLogger
from organizer.operational_health import OperationalHealth

_CAPTURE_SUPPORTED_KINDS = frozenset({"rename", "move", "copy", "archive", "unarchive"})

_DB_BUSY_TIMEOUT_MS = 30_000


class _SevenZipExtractionError(Exception):
    """The system 7z tool failed to extract an archive."""


_ARCHIVE_DECODER_ERRORS = (
    zipfile.BadZipFile,
    lzma.LZMAError,
    zlib.error,
    _Py7zArchiveError,
)

_ARCHIVE_ERRORS = (
    OSError,
    ValueError,
    zipfile.BadZipFile,
    RuntimeError,
    lzma.LZMAError,
    zlib.error,
    _Py7zArchiveError,
    _Py7zPasswordRequired,
    rarfile.Error,
    rarfile.RarCannotExec,
)


def iso_timestamp(value: str) -> str:
    """Normalize a stored timestamp to ISO-8601 UTC.

    Attempt and suppression timestamps were historically persisted as Unix
    epoch strings (e.g. "1786471480.84"). Convert those to the ISO-8601 format
    used elsewhere in the API so date filters and consumers behave uniformly.
    Non-epoch strings are returned unchanged.
    """
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return value


def _open_attempts_db(path: Path) -> sqlite3.Connection:
    """Open the attempts DB with WAL-friendly settings.

    A generous ``timeout``/``busy_timeout`` lets a scanner tick or web request
    wait out a concurrent write burst instead of failing immediately with
    ``sqlite3.OperationalError: database is locked``.
    """
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout=%d" % _DB_BUSY_TIMEOUT_MS)
    return connection


class ExecutionMode(StrEnum):
    APPLY = "apply"
    DRY_RUN = "dry-run"


class BatchItemStatus(StrEnum):
    EXECUTED = "executed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
    OUTSIDE_SNAPSHOT = "outside_snapshot"
    FAILED = "failed"


@dataclass(frozen=True)
class BoundaryPolicy:
    """Mounted path policy used to validate watch and action boundaries."""

    data_roots: tuple[Path, ...] = ()
    config_root: Path | None = None
    watch_roots: tuple[Path, ...] = ()
    allowed_destinations: tuple[Path, ...] = ()
    case_sensitive: bool | None = None
    quarantine_root: Path | None = None
    watch_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanRequest:
    watch_id: str
    watch_root: Path
    item: Path
    rules_path: Path
    boundary_policy: BoundaryPolicy | None = None
    processing_lineage: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    target: Path
    preserve_original: bool = True
    limits: tuple[int, int, int] = (10000, 1024 * 1024 * 1024, 1024 * 1024 * 1024)
    max_depth: int = 0
    delete_empty_dirs: bool = False


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
    watch_root: Path | None = None
    allow_hard_link_removal: bool = False
    source_link_count: int = 0
    processing_lineage: tuple[str, ...] = ()
    boundary_policy: BoundaryPolicy | None = None


@dataclass(frozen=True)
class ActionResult:
    kind: str
    target: Path
    result: str
    detail: str = ""
    source: Path | None = None
    resulting_path: Path | None = None


@dataclass(frozen=True)
class ProcessingLineageHandoff:
    watch_id: str
    resulting_path: Path


@dataclass(frozen=True)
class ExecutionReport:
    status: str
    dry_run: bool
    actions: tuple[ActionResult, ...]
    handoffs: tuple[ProcessingLineageHandoff, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchivePreview:
    extraction_root: Path
    entry_count: int
    total_uncompressed_bytes: int
    truncated: bool


@dataclass(frozen=True)
class ItemSnapshot:
    path: Path
    size: int
    mtime: float


@dataclass(frozen=True)
class BatchItemResult:
    source: Path
    status: BatchItemStatus
    detail: str = ""
    report: ExecutionReport | None = None


@dataclass(frozen=True)
class DiscoveryBatch:
    watch_id: str
    items: tuple[BatchItemResult, ...]
    diagnostics: tuple[str, ...]


class BatchPhase(StrEnum):
    """The phase an in-flight batch is currently in, reported via progress.

    ``SCANNING`` covers discovering and evaluating items (stability, identity,
    planning); ``OPERATING`` covers executing an item's planned actions.
    """

    SCANNING = "scanning"
    OPERATING = "operating"


@dataclass(frozen=True)
class BatchProgress:
    """Snapshot of an in-flight batch's phase, reported by ``process_batch``.

    Written from the batch worker thread into the daemon's per-watch progress
    slot via a callback; read atomically by the web thread.
    """

    phase: BatchPhase
    watch_id: str
    current_item: Path | None = None
    processed: int = 0
    total: int = 0


class NeedsReconciliationError(Exception):
    """Raised when an action has published output but source cleanup is uncertain."""


class _AttemptStore:
    """Internal persistence layer for processing attempts, suppressions, and leases."""

    def __init__(self, attempts_path: Path) -> None:
        self._attempts_path = attempts_path
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        return _open_attempts_db(self._attempts_path)

    def initialize(self) -> None:
        self._attempts_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS processing_attempts (
                attempt_id TEXT PRIMARY KEY,
                watch_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                status TEXT NOT NULL,
                resulting_paths TEXT NOT NULL,
                copy_provenance TEXT,
                action_results TEXT NOT NULL DEFAULT '[]',
                source_fingerprint TEXT NOT NULL DEFAULT ''
                )"""
            )
            for col in ("retry_of_attempt_id", "failure_detail", "processing_lineage"):
                try:
                    connection.execute(f"ALTER TABLE processing_attempts ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            for col_sql in (
                "planned_actions TEXT NOT NULL DEFAULT '[]'",
                "source_size INTEGER NOT NULL DEFAULT 0",
                "source_mtime REAL NOT NULL DEFAULT 0.0",
                "started_at TEXT NOT NULL DEFAULT ''",
                "completed_at TEXT NOT NULL DEFAULT ''",
                "accepted_results TEXT NOT NULL DEFAULT '[]'",
                "abandoned_reason TEXT NOT NULL DEFAULT ''",
                "audit_events TEXT NOT NULL DEFAULT '[]'",
            ):
                try:
                    connection.execute(f"ALTER TABLE processing_attempts ADD COLUMN {col_sql}")
                except sqlite3.OperationalError:
                    pass
            connection.execute(
                """CREATE TABLE IF NOT EXISTS item_observations (
                watch_id TEXT NOT NULL, source_path TEXT NOT NULL, size INTEGER NOT NULL,
                mtime REAL NOT NULL, first_seen_at REAL NOT NULL,
                PRIMARY KEY (watch_id, source_path))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS processing_suppressions (
                watch_id TEXT NOT NULL, source_path TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL, attempt_id TEXT NOT NULL,
                suppressed_at TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY (watch_id, source_path, source_fingerprint))"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS processing_leases (
                watch_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                PRIMARY KEY (watch_id, source_path, source_fingerprint)
                )"""
            )

    def start_attempt(self, attempt_id: str, plan: Plan, retry_of_attempt_id: str | None = None) -> None:
        planned_actions = json.dumps([
            {
                "kind": action.kind,
                "target": str(action.target),
                "preserve_original": action.preserve_original,
                "limits": list(action.limits),
                "max_depth": action.max_depth,
            }
            for action in plan.actions
        ])
        started_at = str(time.time())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, copy_provenance, source_fingerprint, retry_of_attempt_id, processing_lineage, planned_actions, source_size, source_mtime, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, plan.watch_id, str(plan.source), plan.rule_name, "started", "[]", None, plan.source_fingerprint, retry_of_attempt_id, json.dumps(list(plan.processing_lineage)), planned_actions, plan.source_size, plan.source_mtime, started_at),
            )

    def finish_attempt(self, attempt_id: str, status: str, results: list[ActionResult], processing_lineage: tuple[str, ...] = ()) -> None:
        paths = [str(result.resulting_path or result.target) for result in results if result.result == "OK"]
        provenance = next((json.dumps({"source": str(result.source), "result": str(result.resulting_path)}) for result in results if result.kind == "copy" and result.result == "OK"), None)
        action_results = json.dumps([{"kind": result.kind, "target": str(result.target), "result": result.result, "detail": result.detail, "source": str(result.source) if result.source else None, "resulting_path": str(result.resulting_path) if result.resulting_path else None} for result in results])
        failure_detail = results[-1].detail if status == "failed" and results else ""
        completed_at = str(time.time())
        with self._connect() as connection:
            connection.execute(
                "UPDATE processing_attempts SET status = ?, resulting_paths = ?, copy_provenance = ?, action_results = ?, failure_detail = ?, processing_lineage = ?, completed_at = ? WHERE attempt_id = ?",
                (status, json.dumps(paths), provenance, action_results, failure_detail, json.dumps(list(processing_lineage)), completed_at, attempt_id),
            )

    def acquire_lease(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        canonical = str(self._canonical_path(source))
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO processing_leases (watch_id, source_path, source_fingerprint, attempt_id, acquired_at) VALUES (?, ?, ?, ?, ?)",
                    (watch_id, canonical, fingerprint, "", str(time.time())),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release_lease(self, watch_id: str, source: Path, fingerprint: str) -> None:
        canonical = str(self._canonical_path(source))
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM processing_leases WHERE watch_id = ? AND source_path = ? AND source_fingerprint = ?",
                (watch_id, canonical, fingerprint),
            )

    def has_active_lease(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        canonical = str(self._canonical_path(source))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM processing_leases WHERE watch_id = ? AND source_path = ? AND source_fingerprint = ?",
                (watch_id, canonical, fingerprint),
            ).fetchone()
        return int(row[0]) > 0

    def has_completed_attempt(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        canonical = str(self._canonical_path(source))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM processing_attempts WHERE watch_id = ? AND source_path = ? AND source_fingerprint = ? AND status = ?",
                (watch_id, canonical, fingerprint, "completed"),
            ).fetchone()
        return int(row[0]) > 0

    def has_suppressed_attempt(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        canonical = str(self._canonical_path(source))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM processing_suppressions WHERE watch_id = ? AND source_path = ? AND source_fingerprint = ?",
                (watch_id, canonical, fingerprint),
            ).fetchone()
        return int(row[0]) > 0

    def create_suppression(self, watch_id: str, source: Path, fingerprint: str, attempt_id: str, reason: str) -> None:
        canonical = str(self._canonical_path(source))
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO processing_suppressions (watch_id, source_path, source_fingerprint, attempt_id, suppressed_at, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (watch_id, canonical, fingerprint, attempt_id, str(time.time()), reason),
            )

    def clear_suppression(self, watch_id: str, source: Path, fingerprint: str) -> None:
        canonical = str(self._canonical_path(source))
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM processing_suppressions WHERE watch_id = ? AND source_path = ? AND source_fingerprint = ?",
                (watch_id, canonical, fingerprint),
            )

    def list_attempts(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, resulting_paths, copy_provenance, failure_detail, retry_of_attempt_id, processing_lineage FROM processing_attempts ORDER BY rowid"
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            status, paths, provenance, failure_detail, retry_of, lineage_json = row
            entry: dict[str, object] = {"status": status, "resulting_paths": json.loads(paths)}
            if provenance:
                entry["copy_provenance"] = json.loads(provenance)
            if failure_detail:
                entry["failure_detail"] = failure_detail
            if retry_of:
                entry["retry_of_attempt_id"] = retry_of
            if lineage_json:
                entry["processing_lineage"] = json.loads(lineage_json)
            result.append(entry)
        return result

    def list_suppressions(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT watch_id, source_path, source_fingerprint, attempt_id, suppressed_at, reason FROM processing_suppressions ORDER BY suppressed_at"
            ).fetchall()
        return [
            {
                "watch_id": watch_id,
                "source_path": source_path,
                "source_fingerprint": source_fingerprint,
                "attempt_id": attempt_id,
                "suppressed_at": iso_timestamp(suppressed_at),
                "reason": reason,
            }
            for watch_id, source_path, source_fingerprint, attempt_id, suppressed_at, reason in rows
        ]

    def record_audit_event(self, attempt_id: str, command: str, detail: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT audit_events FROM processing_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"attempt not found: {attempt_id}")
            events = json.loads(row[0] or "[]")
            events.append({"command": command, "detail": detail, "at": str(time.time())})
            connection.execute(
                "UPDATE processing_attempts SET audit_events = ? WHERE attempt_id = ?",
                (json.dumps(events), attempt_id),
            )

    def record_accepted_result(self, attempt_id: str, entry: dict[str, object], command: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT accepted_results, audit_events FROM processing_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"attempt not found: {attempt_id}")
            accepted = json.loads(row[0] or "[]")
            accepted.append({**entry, "command": command})
            events = json.loads(row[1] or "[]")
            events.append({"command": command, "detail": str(entry.get("resulting_path", "")), "at": str(time.time())})
            connection.execute(
                "UPDATE processing_attempts SET accepted_results = ?, audit_events = ? WHERE attempt_id = ?",
                (json.dumps(accepted), json.dumps(events), attempt_id),
            )

    def is_stable(self, watch_id: str, snapshot: ItemSnapshot, *, now: float, stability_interval: float) -> bool:
        canonical = str(self._canonical_path(snapshot.path))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT size, mtime, first_seen_at FROM item_observations WHERE watch_id = ? AND source_path = ?",
                (watch_id, canonical),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO item_observations (watch_id, source_path, size, mtime, first_seen_at) VALUES (?, ?, ?, ?, ?)",
                    (watch_id, canonical, snapshot.size, snapshot.mtime, now),
                )
                return stability_interval <= 0.0
            stored_size, stored_mtime, first_seen_at = row
            if stored_size != snapshot.size or stored_mtime != snapshot.mtime:
                connection.execute(
                    "UPDATE item_observations SET size = ?, mtime = ?, first_seen_at = ? WHERE watch_id = ? AND source_path = ?",
                    (snapshot.size, snapshot.mtime, now, watch_id, canonical),
                )
                return stability_interval <= 0.0
            return (now - float(first_seen_at)) >= stability_interval

    def recover_stale_leases(self) -> list[str]:
        recovered: list[str] = []
        with self._connect() as connection:
            leases = connection.execute(
                "SELECT watch_id, source_path, source_fingerprint FROM processing_leases"
            ).fetchall()
            for watch_id, source_path, source_fingerprint in leases:
                rows = connection.execute(
                    "SELECT attempt_id FROM processing_attempts WHERE watch_id = ? AND source_path = ? AND source_fingerprint = ? AND status = ?",
                    (watch_id, source_path, source_fingerprint, "started"),
                ).fetchall()
                for (attempt_id,) in rows:
                    connection.execute(
                        "UPDATE processing_attempts SET status = ? WHERE attempt_id = ?",
                        ("needs-reconciliation", attempt_id),
                    )
                    recovered.append(attempt_id)
                connection.execute(
                    "DELETE FROM processing_leases WHERE watch_id = ? AND source_path = ? AND source_fingerprint = ?",
                    (watch_id, source_path, source_fingerprint),
                )
        return recovered

    def record_collision_attempt(self, attempt_id: str, watch_id: str, canonical: Path, fingerprint: str, error: Exception) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, action_results, source_fingerprint, failure_detail, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, watch_id, str(canonical), "unknown", "failed", "[]", "[]", fingerprint, str(error), str(time.time())),
            )

    def check_writable(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1")
            return True
        except sqlite3.OperationalError:
            return False

    @staticmethod
    def _canonical_path(path: Path) -> Path:
        return Path(os.path.abspath(os.path.normpath(path))).resolve(strict=False)


class ItemProcessor:
    """Plans and executes item actions without exposing storage details to callers."""

    def __init__(
        self,
        attempts_path: Path,
        events: list[dict[str, str]] | None = None,
        logger: StructuredLogger | None = None,
        health_checker: OperationalHealth | None = None,
    ) -> None:
        self._attempts_path = attempts_path
        self._events = events if events is not None else []
        self._logger = logger
        self._health_checker = health_checker
        self._store = _AttemptStore(attempts_path)

    def plan(self, request: PlanRequest) -> Plan:
        policy = request.boundary_policy or BoundaryPolicy()
        item = self._canonical_path(request.item)
        watch_root = self._canonical_path(request.watch_root)
        diagnostics: list[str] = []
        self._validate_policy(policy, watch_root, item)
        lineage = self._build_processing_lineage(request, policy, watch_root)
        matching_rule: tuple[str, list[dict[str, Any]], dict[str, re.Match[str]]] | None = None
        ruleset_revision = self._ruleset_revision(request.rules_path)

        loaded = yaml.safe_load(request.rules_path.read_text()) or {}
        rules = loaded.get("rules", []) if isinstance(loaded, dict) else []
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")

        invalid_earlier: list[str] = []
        rule_level_settings: tuple[bool, bool] = (False, False)
        for index, rule in enumerate(rules):
            try:
                name, conditions, actions, allow_direct_deletion, allow_hard_link_removal = self._validate_rule(rule)
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
                    rule_level_settings = (allow_direct_deletion, allow_hard_link_removal)
                    break
            except (TypeError, ValueError, re.error) as error:
                invalid_earlier.append(f"rule {index + 1} invalid: {error}")

        if matching_rule is None:
            raise ValueError("no valid rule matched item")

        diagnostics.extend(invalid_earlier)
        if invalid_earlier and len(diagnostics) == 1:
            diagnostics[0] = f"disabled earlier rule 1: {diagnostics[0]}"

        rule_name, action_specs, matches = matching_rule
        allow_direct_deletion, allow_hard_link_removal = rule_level_settings
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
                delete_mode = action.get("delete", {})
                if not isinstance(delete_mode, dict):
                    raise ValueError(f"rule {rule_name} delete action must be a mapping")
                mode = delete_mode.get("mode")
                if mode == "quarantine":
                    quarantine_root = policy.quarantine_root if policy else None
                    if not quarantine_root:
                        raise ValueError(f"rule {rule_name} quarantine requires a configured quarantine root")
                    quarantine_root = self._canonical_path(quarantine_root)
                    target = quarantine_root / request.watch_id
                    planned.append(PlannedAction(kind="quarantine", target=target))
                    current = target
                elif mode == "direct":
                    if not allow_direct_deletion:
                        raise ValueError(f"rule {rule_name} direct deletion requires allow_direct_deletion: true")
                    planned.append(PlannedAction(kind="delete", target=current))
                else:
                    raise ValueError(f"rule {rule_name} delete mode must be 'direct' or 'quarantine'")
                continue
            if kind == "archive" and isinstance(action.get(kind), dict):
                archive = action[kind]
                destination = archive.get("destination")
                extension = archive.get("extension", ".zip")
                preserve_original = archive.get("preserve_originals", True)
                if not isinstance(destination, str) or not destination:
                    raise ValueError(f"rule {rule_name} archive destination is required")
                if not isinstance(extension, str) or not extension.startswith(".") or Path(extension).name != extension:
                    raise ValueError(f"rule {rule_name} archive extension is invalid")
                if extension.lower() not in {".zip", ".7z"}:
                    raise ValueError(f"rule {rule_name} archive extension is unsupported")
                if not isinstance(preserve_original, bool):
                    raise ValueError(f"rule {rule_name} archive preserve_original must be boolean")
                root = Path(self._expand_captures(destination, matches))
                if not root.is_absolute():
                    root = watch_root / root
                destination_root = self._resolve_destination(root)
                self._validate_destination(policy, watch_root, current, destination_root)
                target = destination_root / self._archive_output_name(current, extension)
                self._validate_destination_item(target)
                if target.exists() or self._case_collision(target, policy):
                    raise ValueError(f"destination collision: {target}")
                planned.append(PlannedAction(kind="archive", target=target, preserve_original=preserve_original))
                current = target
                continue
            if kind == "unarchive" and isinstance(action.get(kind), dict):
                unarchive = action[kind]
                destination = unarchive.get("destination", ".")
                preserve_original = unarchive.get("preserve_original", True)
                limits = self._archive_limits(unarchive)
                max_depth = int(unarchive.get("max_depth", 0))
                if max_depth < 0:
                    raise ValueError(f"rule {rule_name} unarchive max_depth must be non-negative")
                if not isinstance(destination, str) or not destination:
                    raise ValueError(f"rule {rule_name} unarchive destination is required")
                if not isinstance(preserve_original, bool):
                    raise ValueError(f"rule {rule_name} unarchive preserve_original must be boolean")
                root = Path(self._expand_captures(destination, matches))
                if not root.is_absolute():
                    root = watch_root / root
                destination_root = self._resolve_destination(root)
                self._validate_destination(policy, watch_root, current, destination_root)
                target = destination_root / self._archive_output_name(current, "")
                self._validate_destination_item(target)
                if target.exists() or self._case_collision(target, policy):
                    raise ValueError(f"destination collision: {target}")
                planned.append(PlannedAction(kind="unarchive", target=target, preserve_original=preserve_original, limits=limits, max_depth=max_depth))
                current = target
                continue
            if kind not in {"move", "copy"} or not isinstance(action.get(kind), dict):
                raise ValueError(f"rule {rule_name} has unsupported action")
            params = action[kind]
            destination = params.get("destination")
            delete_empty_dirs = params.get("delete_empty_dirs", False)
            if not isinstance(destination, str) or not destination:
                raise ValueError(f"rule {rule_name} move destination is required")
            if not isinstance(delete_empty_dirs, bool):
                raise ValueError(f"rule {rule_name} delete_empty_dirs must be boolean")
            root = Path(self._expand_captures(destination, matches))
            if not root.is_absolute():
                root = watch_root / root
            destination_root = self._resolve_destination(root)
            self._validate_destination(policy, watch_root, current, destination_root)
            current = destination_root / current.name
            self._validate_destination_item(current)
            if current.exists() or self._case_collision(current, policy):
                raise ValueError(f"destination collision: {current}")
            destination_watch = self._watch_id_for_path(destination_root, policy, request.watch_id)
            if destination_watch is not None:
                if destination_watch in lineage:
                    raise ValueError(f"processing lineage cycle: {destination_watch}")
                lineage = (*lineage, destination_watch)
                diagnostics.append(f"resulting-path handoff to watch {destination_watch}")
            elif destination_root in {self._canonical_path(root) for root in policy.watch_roots}:
                diagnostics.append(f"destination is another watch folder: {destination_root}")
            planned.append(PlannedAction(kind=kind, target=current, delete_empty_dirs=delete_empty_dirs))

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
            watch_root=watch_root,
            allow_hard_link_removal=allow_hard_link_removal,
            source_link_count=stat.st_nlink,
            processing_lineage=lineage,
            boundary_policy=policy,
        )

    def continuation_plan(
        self,
        plan: Plan,
        accepted_action_index: int,
        resulting_path: Path,
        resulting_fingerprint: str,
    ) -> Plan:
        if accepted_action_index < 0 or accepted_action_index >= len(plan.actions):
            raise ValueError(f"action index {accepted_action_index} out of range")
        if plan.rules_path is not None and self._ruleset_revision(plan.rules_path) != plan.ruleset_revision:
            raise ValueError("stale plan: ruleset revision changed")
        source = self._canonical_path(resulting_path)
        if not source.exists():
            raise ValueError("accepted resulting path does not exist")
        if source.is_symlink() or self._fingerprint(source) != resulting_fingerprint:
            raise ValueError("accepted resulting identity no longer matches filesystem evidence")
        actions = plan.actions[accepted_action_index + 1:]
        if not actions:
            raise ValueError("no remaining actions to retry")
        typed_actions: list[PlannedAction] = []
        for action in actions:
            typed_actions.append(action)
        return Plan(
            watch_id=plan.watch_id,
            source=source,
            source_size=source.stat().st_size,
            source_mtime=source.stat().st_mtime,
            rule_name=plan.rule_name,
            actions=tuple(typed_actions),
            diagnostics=plan.diagnostics,
            source_fingerprint=resulting_fingerprint,
            ruleset_revision=plan.ruleset_revision,
            rules_path=plan.rules_path,
            watch_root=plan.watch_root,
            allow_hard_link_removal=plan.allow_hard_link_removal,
            source_link_count=source.stat().st_nlink,
            processing_lineage=plan.processing_lineage,
            boundary_policy=plan.boundary_policy,
        )

    def _build_processing_lineage(self, request: PlanRequest, policy: BoundaryPolicy, watch_root: Path) -> tuple[str, ...]:
        lineage = tuple(request.processing_lineage)
        current_watch = request.watch_id
        if current_watch not in lineage:
            lineage = (*lineage, current_watch)
        return lineage

    def _watch_id_for_path(self, path: Path, policy: BoundaryPolicy, current_watch_id: str | None = None) -> str | None:
        canonical = self._canonical_path(path)
        if len(policy.watch_ids) != len(policy.watch_roots):
            return None
        for watch_id, watch_root in zip(policy.watch_ids, policy.watch_roots):
            if current_watch_id is not None and watch_id == current_watch_id:
                continue
            if self._is_within(canonical, self._canonical_path(watch_root)):
                return watch_id
        return None

    def _compute_handoffs(self, plan: Plan) -> list[ProcessingLineageHandoff]:
        policy = plan.boundary_policy or BoundaryPolicy()
        handoffs: list[ProcessingLineageHandoff] = []
        seen: set[str] = {plan.watch_id}
        for action in plan.actions:
            if action.kind in ("move", "copy"):
                dest_watch = self._watch_id_for_path(action.target.parent, policy, plan.watch_id)
                if dest_watch is not None and dest_watch not in seen:
                    seen.add(dest_watch)
                    handoffs.append(ProcessingLineageHandoff(watch_id=dest_watch, resulting_path=action.target))
        return handoffs

    def _has_destructive_action(self, plan: Plan) -> bool:
        return any(action.kind in ("delete", "quarantine") for action in plan.actions)

    def _validate_plan_source(self, plan: Plan) -> None:
        has_destructive = self._has_destructive_action(plan)
        if not has_destructive:
            self._validate_source(plan)

    def execute(self, plan: Plan, mode: ExecutionMode = ExecutionMode.APPLY, retry_of_attempt_id: str | None = None) -> ExecutionReport:
        if mode is ExecutionMode.DRY_RUN:
            self._validate_plan_source(plan)
            if plan.rules_path is not None and self._ruleset_revision(plan.rules_path) != plan.ruleset_revision:
                raise ValueError("stale plan: ruleset revision changed")
            dry_run_results = tuple(
                ActionResult(action.kind, action.target, "DRY_RUN", "would execute", plan.source, action.target)
                for action in plan.actions
            )
            for result in dry_run_results:
                self._emit(plan, result)
            return ExecutionReport(
                status="dry-run",
                dry_run=True,
                actions=dry_run_results,
                warnings=self._metadata_warnings(),
            )

        self._validate_plan_source(plan)
        if plan.rules_path is not None and self._ruleset_revision(plan.rules_path) != plan.ruleset_revision:
            raise ValueError("stale plan: ruleset revision changed")
        if not self.check_persistence_health():
            raise ValueError("persistence unhealthy: execution paused")
        if not self.acquire_lease(plan.watch_id, plan.source, plan.source_fingerprint):
            raise ValueError("processing lease unavailable")
        attempt_id = str(uuid.uuid4())
        try:
            self._start_attempt(attempt_id, plan, retry_of_attempt_id=retry_of_attempt_id)
            results: list[ActionResult] = []
            warnings = self._metadata_warnings()
            source = plan.source
            try:
                for action in plan.actions:
                    if action.kind in ("delete", "quarantine"):
                        if source.is_file() and source.stat().st_nlink > 1 and not plan.allow_hard_link_removal:
                            raise OSError(f"hard-link removal requires allow_hard_link_removal: true ({source.stat().st_nlink} links)")
                        if self._fingerprint(source) != plan.source_fingerprint:
                            result = ActionResult(action.kind, action.target, "UNCERTAIN", "source fingerprint changed", source=source)
                            self._finish_attempt(attempt_id, "needs-reconciliation", results + [result], plan.processing_lineage)
                            self._emit(plan, result)
                            return ExecutionReport(status="needs-reconciliation", dry_run=False, actions=tuple(results + [result]), warnings=warnings)
                        if action.kind == "delete":
                            if source.is_dir():
                                shutil.rmtree(source)
                            else:
                                source.unlink()
                            result = ActionResult("delete", action.target, "OK", source=source)
                        else:
                            quarantine_base = action.target
                            relative = plan.source.relative_to(plan.watch_root) if plan.watch_root else Path(source.name)
                            actual_target = quarantine_base / attempt_id / relative
                            actual_target.parent.mkdir(parents=True, exist_ok=True)
                            if source.is_dir():
                                shutil.copytree(source, actual_target)
                                shutil.rmtree(source)
                            else:
                                shutil.copy2(source, actual_target)
                                source.unlink()
                            result = ActionResult("quarantine", action.target, "OK", source=source, resulting_path=actual_target)
                            source = actual_target
                    elif action.kind == "copy":
                        self._stage_validate_publish(plan, source, action.target, self._copy_to_staging)
                        result = ActionResult(action.kind, action.target, "OK", source=source, resulting_path=action.target)
                        source = action.target
                    elif action.kind == "archive":
                        self._stage_validate_publish(
                            plan,
                            source,
                            action.target,
                            self._archive_to_staging,
                            enforce_mode_preservation=False,
                        )
                        if not action.preserve_original:
                            try:
                                self._ensure_source_unchanged(plan, source, action, results, attempt_id)
                            except NeedsReconciliationError:
                                return ExecutionReport(status="needs-reconciliation", dry_run=False, actions=tuple(results + [ActionResult(action.kind, action.target, "UNCERTAIN", "source fingerprint changed before removal", source=source, resulting_path=action.target)]), warnings=warnings)
                            self._remove_source(source)
                        result = ActionResult(action.kind, action.target, "OK", source=source, resulting_path=action.target)
                        source = action.target
                    elif action.kind == "unarchive":
                        self._stage_validate_publish(
                            plan,
                            source,
                            action.target,
                            lambda current, target: self._unarchive_to_staging(
                                current, target, action.limits, action.max_depth
                            ),
                        )
                        if not action.preserve_original:
                            try:
                                self._ensure_source_unchanged(plan, source, action, results, attempt_id)
                            except NeedsReconciliationError:
                                return ExecutionReport(status="needs-reconciliation", dry_run=False, actions=tuple(results + [ActionResult(action.kind, action.target, "UNCERTAIN", "source fingerprint changed before removal", source=source, resulting_path=action.target)]), warnings=warnings)
                            self._remove_source(source)
                        result = ActionResult(action.kind, action.target, "OK", source=source, resulting_path=action.target)
                        source = action.target
                    else:
                        action_source = source
                        self._validate_destination_item(action.target)
                        if action.target != source:
                            try:
                                self._move_without_overwrite(source, action.target)
                            except NeedsReconciliationError as error:
                                result = ActionResult(action.kind, action.target, "UNCERTAIN", str(error), source=action_source, resulting_path=action.target)
                                results.append(result)
                                self._finish_attempt(attempt_id, "needs-reconciliation", results, plan.processing_lineage)
                                self._emit(plan, result)
                                return ExecutionReport(status="needs-reconciliation", dry_run=False, actions=tuple(results), warnings=warnings)
                            if action.delete_empty_dirs:
                                self._prune_empty_ancestors(action_source, plan.watch_root)
                        source = action.target
                        result = ActionResult(action.kind, action.target, "OK", source=action_source, resulting_path=source)
                    results.append(result)
                    self._emit(plan, result)
            except _ARCHIVE_ERRORS as error:
                classification = "password-protected archive" if isinstance(error, (RuntimeError, rarfile.PasswordRequired, _Py7zPasswordRequired)) else type(error).__name__
                detail = f"{classification}: {error}"
                result = ActionResult(plan.actions[len(results)].kind, plan.actions[len(results)].target, "FAILED", detail, source=source)
                results.append(result)
                reason = "collision" if isinstance(error, FileExistsError) else "archive-input" if plan.actions[len(results) - 1].kind == "unarchive" else ""
                if reason:
                    self._create_suppression(plan.watch_id, plan.source, plan.source_fingerprint, attempt_id, reason)
                self._finish_attempt(attempt_id, "failed", results, plan.processing_lineage)
                self._emit(plan, result)
                return ExecutionReport(status="failed", dry_run=False, actions=tuple(results), warnings=warnings)

            self._finish_attempt(attempt_id, "completed", results, plan.processing_lineage)
            handoffs = self._compute_handoffs(plan)
            return ExecutionReport(status="completed", dry_run=False, actions=tuple(results), handoffs=tuple(handoffs), warnings=warnings)
        finally:
            self._release_lease(plan.watch_id, plan.source, plan.source_fingerprint)

    def _stage_validate_publish(
        self,
        plan: Plan,
        source: Path,
        target: Path,
        stage: Any,
        *,
        enforce_mode_preservation: bool = True,
    ) -> None:
        staging = stage(source, target)
        try:
            self._validate_source(plan)
        except ValueError as error:
            self._remove_tree(staging)
            raise OSError(str(error)) from error
        source_mode = source.stat().st_mode & 0o7777 if source.is_file() and not source.is_symlink() else None
        self._publish_staged(staging, target)
        if (
            enforce_mode_preservation
            and source_mode is not None
            and not target.is_symlink()
            and target.is_file()
            and target.stat().st_mode & 0o7777 != source_mode
        ):
            raise OSError(f"POSIX mode bits were not preserved: {source} -> {target}")

    @staticmethod
    def _validate_mode_preservation(source: Path, target: Path) -> None:
        """Ensure supported POSIX mode bits survive a published copy."""
        if source.is_symlink() or target.is_symlink():
            return
        source_mode = source.stat().st_mode & 0o7777
        target_mode = target.stat().st_mode & 0o7777
        if source_mode != target_mode:
            raise OSError(f"POSIX mode bits were not preserved: {source} -> {target}")

    def _ensure_source_unchanged(
        self,
        plan: Plan,
        source: Path,
        action: PlannedAction,
        results: list[ActionResult],
        attempt_id: str,
    ) -> None:
        if self._fingerprint(source) == plan.source_fingerprint:
            return
        result = ActionResult(
            action.kind,
            action.target,
            "UNCERTAIN",
            "source fingerprint changed before removal",
            source=source,
            resulting_path=action.target,
        )
        self._finish_attempt(attempt_id, "needs-reconciliation", results + [result], plan.processing_lineage)
        self._emit(plan, result)
        raise NeedsReconciliationError("source fingerprint changed before removal")

    def attempts(self) -> list[dict[str, object]]:
        return self._store.list_attempts()

    def record_audit_event(self, attempt_id: str, command: str, detail: str) -> None:
        self._store.record_audit_event(attempt_id, command, detail)

    def record_accepted_result(self, attempt_id: str, entry: dict[str, object], command: str) -> None:
        self._store.record_accepted_result(attempt_id, entry, command)

    def _start_attempt(self, attempt_id: str, plan: Plan, retry_of_attempt_id: str | None = None) -> None:
        self._store.start_attempt(attempt_id, plan, retry_of_attempt_id)

    def _finish_attempt(self, attempt_id: str, status: str, results: list[ActionResult], processing_lineage: tuple[str, ...] = ()) -> None:
        self._store.finish_attempt(attempt_id, status, results, processing_lineage)

    def _emit(self, plan: Plan, result: ActionResult) -> None:
        event = {
            "watch": plan.watch_id,
            "rule": plan.rule_name,
            "action": result.kind,
            "item": str(plan.source),
            "result": result.result,
            "detail": result.detail,
        }
        self._events.append(event)
        if self._logger is not None:
            level = LogLevel.DRYRUN if result.result == "DRY_RUN" else LogLevel.INFO if result.result == "OK" else LogLevel.ERROR
            log_result = LogResult(result.result) if result.result in {r.value for r in LogResult} else LogResult.FAILED
            self._logger.log(
                LogEntry.create(
                    level=level,
                    watch=plan.watch_id,
                    rule=plan.rule_name,
                    action=result.kind,
                    item=str(plan.source),
                    result=log_result,
                    detail=result.detail,
                )
            )

    def check_persistence_health(self) -> bool:
        """Check if persistence is healthy. Returns True if healthy or no checker configured."""
        if self._health_checker is not None:
            health = self._health_checker.check_persistence(self._attempts_path)
            if not health.tracking_db_writable:
                return False
        return self._store.check_writable()

    def check_watch_folder_health(self, watch_id: str, watch_root: Path) -> bool:
        """Check if a watch folder is healthy. Returns True if healthy or no checker configured."""
        if self._health_checker is None:
            return True
        health = self._health_checker.check_watch_folder(watch_id, watch_root)
        return health.accessible

    def _pause_batch(self, watch_id: str, reason: str, snapshots: list[ItemSnapshot], *, diagnostic: str | None = None) -> DiscoveryBatch:
        """Return a paused discovery batch when a health check fails."""
        paused = tuple(
            BatchItemResult(source=snapshot.path, status=BatchItemStatus.SKIPPED, detail=reason)
            for snapshot in snapshots
        )
        if self._logger is not None:
            for item in paused:
                self._logger.log(
                    LogEntry.create(
                        level=LogLevel.WARN,
                        watch=watch_id,
                        rule="",
                        action="",
                        item=str(item.source),
                        result=LogResult.SKIPPED,
                        detail=reason,
                    )
                )
        batch_diagnostic = diagnostic or reason
        return DiscoveryBatch(
            watch_id=watch_id,
            items=paused,
            diagnostics=(batch_diagnostic,),
        )

    def _copy_to_staging(self, source: Path, target: Path) -> Path:
        staging = target.parent / f".organizer-staging-{uuid.uuid4()}"
        try:
            if source.is_dir():
                shutil.copytree(source, staging)
            else:
                staging.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, staging)
            return staging
        except Exception:
            self._remove_tree(staging)
            raise

    @staticmethod
    def _metadata_warnings() -> tuple[str, ...]:
        return (
            "ownership, ACLs, extended attributes, and platform-specific metadata are not guaranteed to be preserved",
        )

    def _archive_to_staging(self, source: Path, target: Path) -> Path:
        staging = target.parent / f".organizer-staging-{uuid.uuid4()}{target.suffix.lower()}"
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.suffix.lower() == ".7z":
                with py7zr.SevenZipFile(staging, "w") as archive:
                    if source.is_dir():
                        archive.writeall(source, arcname=source.name)
                    else:
                        archive.write(source, arcname=source.name)
                return staging
            with zipfile.ZipFile(staging, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                if source.is_dir():
                    for child in sorted(source.rglob("*")):
                        relative = child.relative_to(source)
                        if child.is_dir():
                            if not any(child.iterdir()):
                                archive.writestr(f"{relative}/", "")
                        elif child.is_symlink():
                            info = zipfile.ZipInfo(str(relative))
                            info.create_system = 3
                            info.external_attr = (0o120777 << 16) | 0o777
                            archive.writestr(info, os.readlink(child))
                        elif child.is_file():
                            archive.write(child, relative)
                else:
                    if source.is_symlink():
                        info = zipfile.ZipInfo(source.name)
                        info.create_system = 3
                        info.external_attr = (0o120777 << 16) | 0o777
                        archive.writestr(info, os.readlink(source))
                    else:
                        archive.write(source, source.name)
            return staging
        except Exception:
            self._remove_tree(staging)
            raise

    def preview(self, plan: Plan) -> ArchivePreview | None:
        action = next((action for action in plan.actions if action.kind == "unarchive"), None)
        if action is None:
            return None
        return self._inspect_archive(plan.source, action.target, action.limits)

    def _inspect_archive(self, source: Path, target: Path, limits: tuple[int, int, int]) -> ArchivePreview:
        max_entries, max_bytes, max_entry_size = limits
        count = total = 0
        for info in self._archive_info(source):
                count += 1
                total += self._entry_size(info)
                entry_size = self._entry_size(info)
                if count > max_entries or total > max_bytes or entry_size > max_entry_size:
                    return ArchivePreview(target, count, total, True)
        return ArchivePreview(target, count, total, False)

    def _unarchive_to_staging(self, source: Path, target: Path, limits: tuple[int, int, int], max_depth: int = 0) -> Path:
        max_entries, max_bytes, max_entry_size = limits
        staging = target.parent / f".organizer-staging-{uuid.uuid4()}"
        staging.mkdir(parents=True)
        try:
            try:
                self._extract_entries_stdlib(source, staging, limits)
            except _ARCHIVE_DECODER_ERRORS as original:
                # The stdlib reader rejected a structurally-inconsistent zip,
                # a mid-stream corrupt deflate stream, or an archive the stdlib
                # decoder is stricter about (e.g. a .7z the strict lzma coder
                # rejects but 7-Zip extracts fine). Fall back to the 7z tool,
                # which tolerates all of these, then validate its output. If
                # 7z also fails the archive is genuinely corrupt — surface the
                # original stdlib error so the diagnostic stays meaningful. A
                # safety rejection from validating 7z's output is NOT masked; it
                # propagates so the attempt fails for the real reason.
                self._remove_tree(staging)
                try:
                    self._extract_with_7z(source, staging)
                except _SevenZipExtractionError as error:
                    raise original from error
                self._validate_staged_archive(staging, limits)
            if max_depth > 0:
                self._extract_nested_archives(staging, limits, max_depth, 1)
            return staging
        except Exception:
            self._remove_tree(staging)
            raise

    def _extract_entries_stdlib(self, source: Path, staging: Path, limits: tuple[int, int, int]) -> None:
        max_entries, max_bytes, max_entry_size = limits
        count = total = 0
        with self._open_archive(source) as archive:
            for info in self._archive_info(source, archive):
                count += 1
                entry_size = self._entry_size(info)
                total += entry_size
                if count > max_entries or total > max_bytes or entry_size > max_entry_size:
                    raise ValueError("archive resource limit exceeded")
                relative = Path(self._entry_name(info))
                destination = staging / relative
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("archive path traversal rejected")
                if self._entry_is_dir(info):
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if self._entry_is_symlink(info):
                    raise ValueError("archive symlink rejected")
                destination.parent.mkdir(parents=True, exist_ok=True)
                copied = 0
                if isinstance(archive, py7zr.SevenZipFile):
                    extracted = archive.read([self._entry_name(info)])
                    input_stream = extracted[self._entry_name(info)] if extracted else None
                    if input_stream is None:
                        raise OSError(f"archive entry unavailable: {self._entry_name(info)}")
                else:
                    input_stream = self._open_entry(archive, info)
                with input_stream, destination.open("wb") as output_stream:
                    while chunk := input_stream.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > max_entry_size or total - entry_size + copied > max_bytes:
                            raise ValueError("archive resource limit exceeded")
                        output_stream.write(chunk)

    def _extract_with_7z(self, source: Path, staging: Path) -> None:
        """Extract ``source`` with the system 7z tool, which tolerates zips the
        stdlib reader rejects and archives the strict stdlib decoder won't
        extract. The caller validates the staged output against the safety
        limits."""
        executable = shutil.which("7z")
        if executable is None:
            raise _SevenZipExtractionError("7z tool unavailable for archive extraction")
        staging.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [executable, "x", "-y", "-bd", f"-o{staging}", "--", str(source)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise _SevenZipExtractionError(f"7z extraction failed: {detail}")

    def _validate_staged_archive(self, staging: Path, limits: tuple[int, int, int]) -> None:
        max_entries, max_bytes, max_entry_size = limits
        count = total = 0
        for path in sorted(staging.rglob("*")):
            if path.is_symlink():
                raise ValueError("archive symlink rejected")
            if path.is_dir():
                continue
            relative = path.relative_to(staging)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("archive path traversal rejected")
            size = path.stat().st_size
            count += 1
            total += size
            if count > max_entries or total > max_bytes or size > max_entry_size:
                raise ValueError("archive resource limit exceeded")

    def _extract_nested_archives(self, tree: Path, limits: tuple[int, int, int], max_depth: int, current_depth: int) -> None:
        if current_depth > max_depth:
            return
        archive_suffixes = {".zip", ".7z", ".rar"}
        nested = sorted(
            child for child in tree.rglob("*")
            if child.is_file() and child.suffix.lower() in archive_suffixes
        )
        for archive_path in nested:
            extraction_root = archive_path.parent / self._archive_output_name(archive_path, "")
            if extraction_root.exists():
                raise FileExistsError(f"nested extraction root collision: {extraction_root}")
            nested_staging = self._unarchive_to_staging(archive_path, extraction_root, limits, max_depth - current_depth)
            extraction_root.parent.mkdir(parents=True, exist_ok=True)
            nested_staging.rename(extraction_root)
            shutil.move(str(archive_path), str(extraction_root / archive_path.name))

    @staticmethod
    def _open_archive(source: Path) -> Any:
        if source.suffix.lower() == ".7z":
            return py7zr.SevenZipFile(source, "r")
        if source.suffix.lower() == ".rar":
            return rarfile.RarFile(source)
        if source.suffix.lower() == ".zip":
            return zipfile.ZipFile(source)
        raise ValueError(f"unsupported archive format: {source.suffix}")

    @classmethod
    def _archive_info(cls, source: Path, archive: Any | None = None) -> list[Any]:
        opened = archive is None
        handle = archive or cls._open_archive(source)
        try:
            if isinstance(handle, zipfile.ZipFile):
                return list(handle.infolist())
            if isinstance(handle, rarfile.RarFile):
                return list(handle.infolist())
            return list(handle.list())
        finally:
            if opened:
                handle.close()

    @staticmethod
    def _entry_name(info: Any) -> str:
        return str(getattr(info, "filename", getattr(info, "name", "")))

    @staticmethod
    def _entry_size(info: Any) -> int:
        return int(getattr(info, "file_size", getattr(info, "uncompressed", 0)))

    @staticmethod
    def _entry_is_dir(info: Any) -> bool:
        return bool(info.is_dir() if hasattr(info, "is_dir") else getattr(info, "is_directory", False))

    @staticmethod
    def _entry_is_symlink(info: Any) -> bool:
        return isinstance(info, zipfile.ZipInfo) and (info.external_attr >> 16) & 0o170000 == 0o120000

    @staticmethod
    def _open_entry(archive: Any, info: Any) -> Any:
        return archive.open(info)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def _archive_limits(settings: dict[str, Any]) -> tuple[int, int, int]:
        values = (settings.get("max_entries", 10000), settings.get("max_uncompressed_bytes", 1024 * 1024 * 1024), settings.get("max_entry_bytes", 1024 * 1024 * 1024))
        if not all(isinstance(value, int) and value > 0 for value in values):
            raise ValueError("unarchive resource limits must be positive integers")
        return values

    @staticmethod
    def _archive_output_name(source: Path, extension: str) -> str:
        recognized = {".zip", ".7z", ".rar"}
        suffix = source.suffix.lower()
        stem = source.name[:-len(suffix)] if suffix in recognized else source.name
        return stem + extension

    @staticmethod
    def _remove_staging(staging: Path) -> None:
        ItemProcessor._remove_tree(staging)

    @staticmethod
    def _remove_source(source: Path) -> None:
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()

    @staticmethod
    def _publish_staged(staging: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if staging.is_dir():
                os.mkdir(target)
                try:
                    for child in staging.iterdir():
                        child.rename(target / child.name)
                    staging.rmdir()
                except OSError:
                    ItemProcessor._remove_tree(target)
                    raise
            else:
                os.link(staging, target)
                staging.unlink()
        except FileExistsError as error:
            ItemProcessor._remove_tree(staging)
            raise FileExistsError(f"destination already exists: {target}") from error

    @staticmethod
    def _move_without_overwrite(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        source_mode = source.stat().st_mode & 0o7777 if source.is_file() and not source.is_symlink() else None
        try:
            if source.is_dir():
                raise OSError(errno.EXDEV, "directory move requires the staged copy path")
            os.link(source, target)
        except FileExistsError as error:
            raise FileExistsError(f"destination already exists: {target}") from error
        except OSError as error:
            if getattr(error, "errno", None) != errno.EXDEV:
                raise
            staging = target.parent / f".organizer-staging-{uuid.uuid4()}"
            try:
                if source.is_dir():
                    shutil.copytree(source, staging)
                else:
                    shutil.copy2(source, staging)
                ItemProcessor._publish_staged(staging, target)
                if source_mode is not None and not target.is_symlink() and target.is_file() and target.stat().st_mode & 0o7777 != source_mode:
                    raise OSError(f"POSIX mode bits were not preserved: {source} -> {target}")
            except FileExistsError as error:
                ItemProcessor._remove_tree(staging)
                raise FileExistsError(f"destination already exists: {target}") from error
        try:
            if source.is_dir():
                shutil.rmtree(source)
            else:
                source.unlink()
        except OSError as error:
            raise NeedsReconciliationError(f"source removal uncertain: {error}") from error

    @classmethod
    def _prune_empty_ancestors(cls, source: Path, watch_root: Path | None) -> None:
        """Remove now-empty ancestor directories of a removed item.

        Walks upward from ``source.parent`` toward (but not including) the watch
        root, removing each directory once it is empty. Stops at the first
        non-empty directory. When no watch root is available, does nothing.
        """
        if watch_root is None:
            return
        root = watch_root.resolve()
        current = source.parent.resolve()
        try:
            while current != root and root in current.parents:
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent
        except OSError:
            return

    @classmethod
    def validate_rules_document(cls, rules_path: Path, policy: BoundaryPolicy | None = None, watch_root: Path | None = None) -> list[str]:
        """Validate a rules document without requiring an item match.

        When *policy* and *watch_root* are provided, destination-boundary
        validation is also performed.  Returns a list of diagnostics; an empty
        list means the document is valid.
        """
        diagnostics: list[str] = []
        try:
            loaded = yaml.safe_load(rules_path.read_text()) or {}
        except (OSError, yaml.YAMLError) as error:
            return [f"invalid rules document: {error}"]
        if not isinstance(loaded, dict):
            return ["rules document must be a mapping"]
        rules = loaded.get("rules", [])
        if not isinstance(rules, list):
            return ["rules must be a list"]
        for index, rule in enumerate(rules):
            rule_name = rule.get("name") if isinstance(rule, dict) else "unknown"
            rule_name = rule_name if isinstance(rule_name, str) else "unknown"
            try:
                name, conditions, actions, allow_direct_deletion, _ = cls._validate_rule(rule)
                cls._validate_action_params(actions, name, allow_direct_deletion)
                cls._validate_capture_references_pattern(actions, conditions)
                if policy is not None and watch_root is not None:
                    cls._validate_rule_destinations(actions, name, policy, watch_root)
            except (TypeError, ValueError, re.error) as error:
                diagnostics.append(f"rule {index + 1} ({rule_name}) invalid: {error}")
        return diagnostics

    @staticmethod
    def _validate_action_params(actions: list[dict[str, Any]], rule_name: str, allow_direct_deletion: bool) -> None:
        if not isinstance(actions, list) or not actions:
            raise ValueError("actions are required")
        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                raise ValueError("each action must be a mapping")
            if len(action) != 1:
                raise ValueError("each action must have exactly one key")
            kind = next(iter(action))
            params = action[kind]
            if not isinstance(params, dict):
                raise ValueError("action params must be a mapping")
            if kind == "rename":
                name = params.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("rename name is required")
            elif kind == "delete":
                if i != len(actions) - 1:
                    raise ValueError("delete result cannot accept a later action")
                mode = params.get("mode")
                if mode == "direct":
                    if not allow_direct_deletion:
                        raise ValueError("direct deletion requires allow_direct_deletion: true")
                elif mode == "quarantine":
                    pass
                else:
                    raise ValueError("delete mode must be 'direct' or 'quarantine'")
            elif kind == "archive":
                destination = params.get("destination")
                if not isinstance(destination, str) or not destination:
                    raise ValueError("archive destination is required")
                extension = params.get("extension", ".zip")
                if not isinstance(extension, str) or not extension.startswith("."):
                    raise ValueError("archive extension is invalid")
                if extension.lower() not in {".zip", ".7z"}:
                    raise ValueError("archive extension is unsupported")
                if "preserve_originals" in params and not isinstance(params["preserve_originals"], bool):
                    raise ValueError("archive preserve_originals must be boolean")
            elif kind == "unarchive":
                destination = params.get("destination", ".")
                if not isinstance(destination, str) or not destination:
                    raise ValueError("unarchive destination is required")
                if "preserve_original" in params and not isinstance(params["preserve_original"], bool):
                    raise ValueError("unarchive preserve_original must be boolean")
                if "max_depth" in params:
                    max_depth = params["max_depth"]
                    if not isinstance(max_depth, int) or max_depth < 0:
                        raise ValueError("unarchive max_depth must be non-negative integer")
            elif kind in ("move", "copy"):
                destination = params.get("destination")
                if not isinstance(destination, str) or not destination:
                    raise ValueError("action destination is required")
            else:
                raise ValueError(f"unsupported action: {kind}")

    @staticmethod
    def _validate_capture_references_pattern(actions: list[dict[str, Any]], conditions: dict[str, tuple[str, str]]) -> None:
        condition_patterns: dict[str, re.Pattern[str]] = {}
        for condition_name, (field, pattern_str) in conditions.items():
            condition_patterns[condition_name] = re.compile(pattern_str)
        for action in actions:
            kind = next(iter(action), "")
            if kind in _CAPTURE_SUPPORTED_KINDS and isinstance(action[kind], dict):
                field = ItemProcessor._capture_field_for(kind)
                name = action[kind].get(field)
                if not isinstance(name, str) or not name:
                    raise ValueError(f"{kind} {field} is required")
                for reference in re.findall(
                    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*)\.)?\\(?:[1-9][0-9]*|g<[^>]+>)", name
                ):
                    condition_name, capture = ItemProcessor._split_capture_reference(reference)
                    compiled = condition_patterns.get(condition_name)
                    if compiled is None:
                        raise ValueError(f"condition '{condition_name}' not found for capture reference")
                    if "g<" in capture:
                        group_name = capture.split("g<", 1)[1].rstrip(">")
                        if group_name not in compiled.groupindex:
                            raise ValueError(f"invalid capture reference: group '{group_name}' does not exist in pattern")
                    else:
                        group_num = int(capture.lstrip("\\"))
                        if group_num < 1 or group_num > compiled.groups:
                            raise ValueError(f"invalid capture reference: group {group_num} does not exist in pattern")

    @classmethod
    def _validate_rule_destinations(cls, actions: list[dict[str, Any]], rule_name: str, policy: BoundaryPolicy, watch_root: Path) -> None:
        for action in actions:
            kind = next(iter(action))
            params = action[kind]
            if kind == "archive":
                destination = params.get("destination", "")
                cls._check_destination_str(destination, watch_root, policy, rule_name)
            elif kind == "unarchive":
                destination = params.get("destination", ".")
                cls._check_destination_str(destination, watch_root, policy, rule_name)
            elif kind in ("move", "copy"):
                destination = params.get("destination", "")
                cls._check_destination_str(destination, watch_root, policy, rule_name)

    @classmethod
    def _check_destination_str(cls, destination: str, watch_root: Path, policy: BoundaryPolicy, rule_name: str) -> None:
        root = Path(destination)
        if not root.is_absolute():
            root = watch_root / root
        destination_root = cls._resolve_destination(root)
        cls._validate_destination_root(policy, destination_root)

    @staticmethod
    def _validate_rule(rule: object) -> tuple[str, dict[str, tuple[str, str]], list[dict[str, Any]], bool, bool]:
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
            conditions = {}
        elif not isinstance(conditions, dict) or not conditions:
            raise ValueError("conditions are required")
        merged = dict(conditions)
        merged["match"] = match
        conditions = merged
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
        allow_direct_deletion = bool(rule.get("allow_direct_deletion", False))
        allow_hard_link_removal = bool(rule.get("allow_hard_link_removal", False))
        return name, parsed_conditions, actions, allow_direct_deletion, allow_hard_link_removal

    @staticmethod
    def _ruleset_revision(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _validate_action_references(actions: list[dict[str, Any]], matches: dict[str, re.Match[str]]) -> None:
        for action in actions:
            kind = next(iter(action), "")
            if kind in _CAPTURE_SUPPORTED_KINDS and isinstance(action[kind], dict):
                field = ItemProcessor._capture_field_for(kind)
                name = action[kind].get(field)
                if not isinstance(name, str) or not name:
                    raise ValueError(f"{kind} {field} is required")
                try:
                    for reference in re.findall(
                        r"(?:(?:[A-Za-z_][A-Za-z0-9_]*)\.)?\\(?:[1-9][0-9]*|g<[^>]+>)", name
                    ):
                        condition_name, capture = ItemProcessor._split_capture_reference(reference)
                        if condition_name not in matches:
                            raise ValueError(f"condition '{condition_name}' not found for capture reference")
                        matches[condition_name].expand(capture)
                except (IndexError, re.error, ValueError) as error:
                    raise ValueError(f"invalid capture reference: {error}") from error

    @staticmethod
    def _capture_field_for(kind: str) -> str:
        return "name" if kind == "rename" else "destination"

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

    def acquire_lease(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        return self._store.acquire_lease(watch_id, source, fingerprint)

    def _release_lease(self, watch_id: str, source: Path, fingerprint: str) -> None:
        self._store.release_lease(watch_id, source, fingerprint)

    def _has_active_lease(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        return self._store.has_active_lease(watch_id, source, fingerprint)

    def has_completed_attempt(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        return self._store.has_completed_attempt(watch_id, source, fingerprint)

    def has_suppressed_attempt(self, watch_id: str, source: Path, fingerprint: str) -> bool:
        return self._store.has_suppressed_attempt(watch_id, source, fingerprint)

    def _create_suppression(self, watch_id: str, source: Path, fingerprint: str, attempt_id: str, reason: str) -> None:
        self._store.create_suppression(watch_id, source, fingerprint, attempt_id, reason)

    def clear_suppression(self, watch_id: str, source: Path, fingerprint: str) -> None:
        self._store.clear_suppression(watch_id, source, fingerprint)

    def suppressed_attempts(self) -> list[dict[str, object]]:
        return self._store.list_suppressions()

    def retry_attempt(
        self,
        attempt_id: str,
        watch_root: Path,
        rules_path: Path,
        boundary_policy: BoundaryPolicy | None = None,
    ) -> ExecutionReport:
        db_watch_id, source, source_fingerprint, status = self._lookup_attempt(
            self._attempts_path, attempt_id
        )
        if status not in ("failed", "needs-reconciliation"):
            raise ValueError(f"attempt {attempt_id} is not retryable: {status}")
        request = PlanRequest(
            watch_id=db_watch_id,
            watch_root=self._canonical_path(watch_root),
            item=source,
            rules_path=rules_path,
            boundary_policy=boundary_policy,
        )
        plan = self.plan(request)
        self.clear_suppression(db_watch_id, source, source_fingerprint)
        return self.execute(plan, retry_of_attempt_id=attempt_id)

    def reprocess_item(
        self,
        watch_id: str,
        watch_root: Path,
        item: Path,
        rules_path: Path,
        boundary_policy: BoundaryPolicy | None = None,
    ) -> ExecutionReport:
        source = self._canonical_path(item)
        if not source.exists():
            raise ValueError(f"source no longer exists: {source}")
        fingerprint = self._fingerprint(source)
        self.clear_suppression(watch_id, source, fingerprint)
        request = PlanRequest(
            watch_id=watch_id,
            watch_root=self._canonical_path(watch_root),
            item=source,
            rules_path=rules_path,
            boundary_policy=boundary_policy,
        )
        plan = self.plan(request)
        return self.execute(plan)

    def reprocess_attempt(
        self,
        attempt_id: str,
        watch_root: Path,
        rules_path: Path,
        boundary_policy: BoundaryPolicy | None = None,
    ) -> ExecutionReport:
        """Re-run a completed or suppressed attempt with a fresh plan.

        Unlike ``retry_attempt``, this deliberately has no status gate: stale
        ``completed`` records from an old rules era and suppressed items should
        both be re-planned and re-executed so a corrected rule can take effect.
        """
        db_watch_id, source, _fingerprint, _status = self._lookup_attempt(
            self._attempts_path, attempt_id
        )
        return self.reprocess_item(
            watch_id=db_watch_id,
            watch_root=watch_root,
            item=source,
            rules_path=rules_path,
            boundary_policy=boundary_policy,
        )

    @staticmethod
    def _lookup_attempt(
        attempts_path: Path, attempt_id: str
    ) -> tuple[str, Path, str, str]:
        """Resolve an attempt to (watch_id, canonical source, fingerprint, status)."""
        with _open_attempts_db(attempts_path) as connection:
            row = connection.execute(
                "SELECT watch_id, source_path, source_fingerprint, status FROM processing_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"attempt not found: {attempt_id}")
        db_watch_id, source_path, source_fingerprint, status = row
        return db_watch_id, ItemProcessor._canonical_path(Path(source_path)), source_fingerprint, status

    def is_stable(self, watch_id: str, snapshot: ItemSnapshot, *, now: float, stability_interval: float) -> bool:
        return self._store.is_stable(watch_id, snapshot, now=now, stability_interval=stability_interval)

    def process_batch(
        self,
        watch_id: str,
        watch_root: Path,
        rules_path: Path,
        snapshots: list[ItemSnapshot],
        *,
        stability_interval: float = 0.0,
        boundary_policy: BoundaryPolicy | None = None,
        now: float | None = None,
        dry_run: bool = False,
        progress: Callable[[BatchProgress], None] | None = None,
    ) -> DiscoveryBatch:
        current_time = now if now is not None else time.time()
        total = len(snapshots)
        processed = 0

        def emit_progress(phase: BatchPhase, current_item: Path | None = None) -> None:
            if progress is not None:
                progress(
                    BatchProgress(
                        phase=phase,
                        watch_id=watch_id,
                        current_item=current_item,
                        processed=processed,
                        total=total,
                    )
                )

        emit_progress(BatchPhase.SCANNING)

        if not self.check_watch_folder_health(watch_id, watch_root):
            return self._pause_batch(watch_id, "watch folder unhealthy: paused", snapshots, diagnostic="watch folder unhealthy: processing paused")

        if not dry_run and not self.check_persistence_health():
            return self._pause_batch(watch_id, "persistence unhealthy: execution paused", snapshots)

        results: list[BatchItemResult] = []
        diagnostics_set: set[str] = set()
        has_deferred = False
        no_match_sources: list[Path] = []
        quarantine_root = boundary_policy.quarantine_root if boundary_policy else None
        for snapshot in snapshots:
            canonical = self._canonical_path(snapshot.path)
            emit_progress(BatchPhase.SCANNING, canonical)
            if quarantine_root and self._is_within(canonical, self._canonical_path(quarantine_root)):
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.SKIPPED, detail="organizer-managed path"))
                continue
            try:
                stable = self.is_stable(watch_id, snapshot, now=current_time, stability_interval=stability_interval)
                if not stable:
                    detail = (
                        f"unstable: first observation at {time.strftime('%H:%M:%S', time.localtime(current_time))}, "
                        f"stability interval {stability_interval:g}s; will re-check on next scan tick"
                    )
                    results.append(BatchItemResult(source=canonical, status=BatchItemStatus.DEFERRED, detail=detail))
                    has_deferred = True
                    continue
                fingerprint = self._fingerprint(canonical)
            except FileNotFoundError as error:
                results.append(
                    BatchItemResult(
                        source=canonical,
                        status=BatchItemStatus.SKIPPED,
                        detail=f"item disappeared during scan: {error}",
                    )
                )
                continue
            except OSError as error:
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.FAILED, detail=str(error)))
                continue
            if self.has_completed_attempt(watch_id, canonical, fingerprint):
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.SKIPPED, detail="already completed"))
                continue
            if self.has_suppressed_attempt(watch_id, canonical, fingerprint):
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.FAILED, detail="suppressed: collision"))
                continue
            if self._has_active_lease(watch_id, canonical, fingerprint):
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.OUTSIDE_SNAPSHOT, detail="already leased"))
                continue
            request = PlanRequest(
                watch_id=watch_id,
                watch_root=watch_root,
                item=canonical,
                rules_path=rules_path,
                boundary_policy=boundary_policy,
            )
            try:
                plan = self.plan(request)
                mode = ExecutionMode.DRY_RUN if dry_run else ExecutionMode.APPLY
                emit_progress(BatchPhase.OPERATING, canonical)
                report = self.execute(plan, mode)
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.EXECUTED, report=report))
            except ValueError as error:
                is_collision = "collision" in str(error).lower()
                if is_collision:
                    attempt_id = str(uuid.uuid4())
                    self._create_suppression(watch_id, canonical, fingerprint, attempt_id, "collision")
                    self._store.record_collision_attempt(attempt_id, watch_id, canonical, fingerprint, error)
                elif "no valid rule matched" in str(error):
                    no_match_sources.append(canonical)
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.FAILED, detail=str(error)))
            except Exception as error:
                results.append(BatchItemResult(source=canonical, status=BatchItemStatus.FAILED, detail=f"{type(error).__name__}: {error}"))
            processed += 1
        if has_deferred:
            diagnostics_set.add("deferred: unstable items withheld from planning")
        if no_match_sources:
            names = ", ".join(path.name for path in no_match_sources[:5])
            suffix = "" if len(no_match_sources) <= 5 else f" (and {len(no_match_sources) - 5} more)"
            diagnostics_set.add(
                f"{len(no_match_sources)} item(s) matched no rule: {names}{suffix}"
            )
        if self._logger is not None:
            for diagnostic in diagnostics_set:
                if "matched no rule" in diagnostic or "deferred: unstable" in diagnostic:
                    self._logger.log(
                        LogEntry.create(
                            level=LogLevel.WARN,
                            watch=watch_id,
                            rule="",
                            action="scan",
                            item="",
                            result=LogResult.SKIPPED,
                            detail=diagnostic,
                        )
                    )
        return DiscoveryBatch(
            watch_id=watch_id,
            items=tuple(results),
            diagnostics=tuple(diagnostics_set),
        )

    def recover_stale_leases(self) -> list[str]:
        return self._store.recover_stale_leases()
