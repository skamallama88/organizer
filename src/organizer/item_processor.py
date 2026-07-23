from __future__ import annotations

import json
import re
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
class PlanRequest:
    watch_id: str
    watch_root: Path
    item: Path
    rules_path: Path


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


@dataclass(frozen=True)
class ActionResult:
    kind: str
    target: Path
    result: str
    detail: str = ""


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
        item = request.item.resolve()
        watch_root = request.watch_root.resolve()
        diagnostics: list[str] = []
        matching_rule: tuple[str, list[dict[str, Any]]] | None = None

        loaded = yaml.safe_load(request.rules_path.read_text()) or {}
        rules = loaded.get("rules", []) if isinstance(loaded, dict) else []
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")

        for index, rule in enumerate(rules):
            try:
                name, field, pattern, actions = self._validate_rule(rule)
                value = self._match_value(field, item)
                if re.search(pattern, value):
                    matching_rule = (name, actions)
                    break
            except (TypeError, ValueError, re.error) as error:
                diagnostics.append(f"rule {index + 1} invalid: {error}")

        if matching_rule is None:
            raise ValueError("no valid rule matched item")

        rule_name, action_specs = matching_rule
        planned: list[PlannedAction] = []
        current = item
        for action in action_specs:
            if set(action) != {"move"} or not isinstance(action["move"], dict):
                raise ValueError(f"rule {rule_name} has unsupported action")
            destination = action["move"].get("destination")
            if not isinstance(destination, str) or not destination:
                raise ValueError(f"rule {rule_name} move destination is required")
            root = Path(destination)
            if not root.is_absolute():
                root = watch_root / root
            current = root.resolve() / current.name
            planned.append(PlannedAction(kind="move", target=current))

        stat = item.stat()
        return Plan(
            watch_id=request.watch_id,
            source=item,
            source_size=stat.st_size,
            source_mtime=stat.st_mtime,
            rule_name=rule_name,
            actions=tuple(planned),
            diagnostics=tuple(diagnostics),
        )

    def execute(self, plan: Plan, mode: ExecutionMode = ExecutionMode.APPLY) -> ExecutionReport:
        if mode is ExecutionMode.DRY_RUN:
            dry_run_results = tuple(
                ActionResult(action.kind, action.target, "DRY_RUN", "would execute") for action in plan.actions
            )
            for result in dry_run_results:
                self._emit(plan, result)
            return ExecutionReport(status="dry-run", dry_run=True, actions=dry_run_results)

        self._validate_source(plan)
        attempt_id = str(uuid.uuid4())
        self._start_attempt(attempt_id, plan)
        results: list[ActionResult] = []
        source = plan.source
        try:
            for action in plan.actions:
                if action.kind != "move":
                    raise ValueError(f"unsupported action {action.kind}")
                if action.target.exists():
                    raise FileExistsError(f"destination already exists: {action.target}")
                action.target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(action.target)
                source = action.target
                result = ActionResult(action.kind, action.target, "OK")
                results.append(result)
                self._emit(plan, result)
        except OSError as error:
            result = ActionResult(plan.actions[len(results)].kind, plan.actions[len(results)].target, "FAILED", str(error))
            results.append(result)
            self._finish_attempt(attempt_id, "failed", results)
            self._emit(plan, result)
            return ExecutionReport(status="failed", dry_run=False, actions=tuple(results))

        self._finish_attempt(attempt_id, "completed", results)
        return ExecutionReport(status="completed", dry_run=False, actions=tuple(results))

    def attempts(self) -> list[dict[str, object]]:
        with sqlite3.connect(self._attempts_path) as connection:
            rows = connection.execute(
                "SELECT status, resulting_paths FROM processing_attempts ORDER BY rowid"
            ).fetchall()
        return [{"status": status, "resulting_paths": json.loads(paths)} for status, paths in rows]

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
                resulting_paths TEXT NOT NULL
                )"""
            )

    def _start_attempt(self, attempt_id: str, plan: Plan) -> None:
        with sqlite3.connect(self._attempts_path) as connection:
            connection.execute(
                "INSERT INTO processing_attempts VALUES (?, ?, ?, ?, ?, ?)",
                (attempt_id, plan.watch_id, str(plan.source), plan.rule_name, "started", "[]"),
            )

    def _finish_attempt(self, attempt_id: str, status: str, results: list[ActionResult]) -> None:
        paths = [str(result.target) for result in results if result.result == "OK"]
        with sqlite3.connect(self._attempts_path) as connection:
            connection.execute(
                "UPDATE processing_attempts SET status = ?, resulting_paths = ? WHERE attempt_id = ?",
                (status, json.dumps(paths), attempt_id),
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

    @staticmethod
    def _validate_rule(rule: object) -> tuple[str, str, str, list[dict[str, Any]]]:
        if not isinstance(rule, dict):
            raise ValueError("rule must be a mapping")
        name = rule.get("name")
        match = rule.get("match")
        actions = rule.get("actions")
        if not isinstance(name, str) or not name:
            raise ValueError("name is required")
        if not isinstance(match, dict):
            raise ValueError("match is required")
        field = match.get("field")
        pattern = match.get("pattern")
        if field not in {"file_name", "folder_name", "full_path"}:
            raise ValueError("match field is invalid")
        if not isinstance(pattern, str):
            raise ValueError("match pattern is required")
        re.compile(pattern)
        if not isinstance(actions, list) or not actions:
            raise ValueError("actions are required")
        return name, field, pattern, actions

    @staticmethod
    def _match_value(field: str, item: Path) -> str:
        if field == "full_path":
            return str(item)
        if field == "folder_name":
            return item.name if item.is_dir() else item.parent.name
        return item.name

    @staticmethod
    def _validate_source(plan: Plan) -> None:
        stat = plan.source.stat()
        if stat.st_size != plan.source_size or stat.st_mtime != plan.source_mtime:
            raise ValueError("stale plan: source changed")
