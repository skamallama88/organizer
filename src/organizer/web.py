from pathlib import Path
import hashlib
import html
import json
import os
import threading
import uuid
import yaml

from fastapi import Body, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from organizer.attempt_review import (
    Abandon,
    Accept,
    AttemptFilters,
    AttemptReviewDetails,
    AttemptReview,
    Reopen,
    RetryFromStart,
)
from organizer.config import WatchFolderConfig
from organizer.item_processor import ExecutionMode, ItemProcessor, PlanRequest
from organizer.operational_health import OperationalHealth
from organizer.structured_log import LogLevel, MemoryLogSink

__all__ = ["WatchFolderConfig", "create_app"]

_WEB_ROOT = Path(__file__).parent / "web_ui"
_TEMPLATES = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))
_RULE_SAVE_LOCK = threading.Lock()


def create_app(
    processor: ItemProcessor,
    *,
    log_sink: MemoryLogSink | None = None,
    health_checker: OperationalHealth | None = None,
    watch_folders: list[WatchFolderConfig] | tuple[WatchFolderConfig, ...] | None = None,
    db_path: Path | None = None,
) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")
    review = AttemptReview(processor)

    def _watch_config(watch_id: str) -> WatchFolderConfig | None:
        if watch_folders is None:
            return None
        for config in watch_folders:
            if config.watch_id == watch_id:
                return config
        return None

    def _rule_count(rules_path: Path) -> int:
        try:
            document = yaml.safe_load(rules_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return 0
        rules = document.get("rules", []) if isinstance(document, dict) else []
        return len(rules) if isinstance(rules, list) else 0

    def _health_by_watch() -> dict[str, tuple[bool, str]]:
        if health_checker is None or db_path is None or watch_folders is None:
            return {}
        health = health_checker.check_all(
            watch_folders=[(config.watch_id, config.watch_root) for config in watch_folders],
            db_path=db_path,
        )
        return {entry.watch_id: (entry.accessible, entry.detail) for entry in health.watch_folder_healths}

    def _fragment(request: Request, template: str, **context: object) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, template, context)

    def _rules_feedback(message: str, *, error: bool = False) -> str:
        css_class = "feedback error" if error else "feedback success"
        return f'<p class="{css_class}">{html.escape(message)}</p>'

    def _attempt_payload(details: AttemptReviewDetails) -> dict[str, object]:
        return {
            "attempt_id": details.attempt_id,
            "watch_id": details.watch_id,
            "source_path": details.source_path,
            "source_fingerprint": details.source_fingerprint,
            "source_size": details.source_size,
            "source_mtime": details.source_mtime,
            "rule_name": details.rule_name,
            "status": details.status,
            "planned_actions": list(details.planned_actions),
            "action_results": list(details.action_results),
            "filesystem_evidence": list(details.filesystem_evidence),
            "resulting_paths": list(details.resulting_paths),
            "failure_detail": details.failure_detail,
            "suppressions": list(details.suppressions),
            "linked_attempts": list(details.linked_attempts),
            "processing_lineage": list(details.processing_lineage),
            "accepted_results": list(details.accepted_results),
            "abandoned_reason": details.abandoned_reason,
            "created_at": details.created_at,
            "completed_at": details.completed_at,
        }

    def _is_html_request(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true" or "text/html" in request.headers.get("accept", "")

    def _form_or_json(body: bytes) -> dict[str, object]:
        try:
            return dict(json.loads(body))
        except (json.JSONDecodeError, TypeError, ValueError):
            from urllib.parse import parse_qs
            return {key: values[-1] for key, values in parse_qs(body.decode()).items()}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        health_by_watch = _health_by_watch()
        watches = []
        for config in watch_folders or ():
            accessible, detail = health_by_watch.get(config.watch_id, (True, ""))
            entries = log_sink.read_recent(limit=1, watch=config.watch_id) if log_sink is not None else []
            watches.append({
                "id": config.watch_id,
                "root": config.watch_root,
                "healthy": accessible and not processor.validate_rules_document(config.rules_path),
                "health_detail": detail,
                "rule_count": _rule_count(config.rules_path),
                "recent_activity": entries[-1] if entries else None,
            })
        return _fragment(request, "dashboard.html", watches=watches)

    @app.get("/watches/{watch_id}/rules", response_class=HTMLResponse)
    def rule_editor(request: Request, watch_id: str) -> HTMLResponse:
        config = _watch_config(watch_id)
        if config is None:
            raise HTTPException(status_code=404, detail="watch folder not configured")
        try:
            rules = config.rules_path.read_text()
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"cannot read rules: {error}") from error
        revision = hashlib.sha256(rules.encode()).hexdigest()
        return _fragment(request, "rule_editor.html", watch=config, rules=rules, revision=revision)

    @app.post("/watches/{watch_id}/rules/validate", response_class=HTMLResponse)
    def validate_rules(request: Request, watch_id: str, rules: str = Form()) -> HTMLResponse:
        config = _watch_config(watch_id)
        if config is None:
            raise HTTPException(status_code=404, detail="watch folder not configured")
        temp_path = config.rules_path.with_suffix(f"{config.rules_path.suffix}.validate-{uuid.uuid4().hex}")
        try:
            temp_path.write_text(rules)
            diagnostics = processor.validate_rules_document(temp_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        if diagnostics:
            return HTMLResponse(_rules_feedback("; ".join(diagnostics), error=True), status_code=422)
        return HTMLResponse(_rules_feedback("Rules are valid."))

    @app.post("/watches/{watch_id}/rules/dry-run", response_class=HTMLResponse)
    def editor_dry_run(request: Request, watch_id: str, item: Path = Form(), rules: str = Form()) -> HTMLResponse:
        config = _watch_config(watch_id)
        if config is None:
            raise HTTPException(status_code=404, detail="watch folder not configured")
        temp_path = config.rules_path.with_suffix(f"{config.rules_path.suffix}.preview-{uuid.uuid4().hex}")
        try:
            temp_path.write_text(rules)
            diagnostics = processor.validate_rules_document(temp_path)
            if diagnostics:
                return HTMLResponse(_rules_feedback("; ".join(diagnostics), error=True), status_code=422)
            plan = processor.plan(PlanRequest(watch_id, config.watch_root, item, temp_path, config.boundary_policy))
            report = processor.execute(plan, ExecutionMode.DRY_RUN)
        except ValueError as error:
            if str(error) == "no valid rule matched item":
                return HTMLResponse(_rules_feedback("Dry run: No rule matched."))
            return HTMLResponse(_rules_feedback(str(error), error=True), status_code=422)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        if not report.actions:
            return HTMLResponse(_rules_feedback("Dry run: No rule matched."))
        rows = "".join(
            f"<li>{html.escape(action.kind)}: {html.escape(str(action.source))} to {html.escape(str(action.target))}</li>"
            for action in report.actions
        )
        return HTMLResponse(f"<section class=\"preview\"><h2>Dry run</h2><ul>{rows}</ul></section>")

    @app.post("/watches/{watch_id}/rules/save", response_class=HTMLResponse)
    def editor_save(
        request: Request,
        watch_id: str,
        rules: str = Form(),
        expected_revision: str = Form(),
    ) -> HTMLResponse:
        config = _watch_config(watch_id)
        if config is None:
            raise HTTPException(status_code=404, detail="watch folder not configured")
        rules_path = config.rules_path
        temp_path = rules_path.with_suffix(f"{rules_path.suffix}.tmp-{uuid.uuid4().hex}")
        try:
            with _RULE_SAVE_LOCK:
                temp_path.write_text(rules)
                diagnostics = processor.validate_rules_document(temp_path)
                if diagnostics:
                    return HTMLResponse(_rules_feedback("; ".join(diagnostics), error=True), status_code=422)
                try:
                    current_revision = hashlib.sha256(rules_path.read_bytes()).hexdigest()
                except OSError as error:
                    return HTMLResponse(_rules_feedback(f"cannot read current rules: {error}", error=True), status_code=500)
                if current_revision != expected_revision:
                    return HTMLResponse(_rules_feedback("Ruleset revision conflict. Reload before saving.", error=True), status_code=409)
                rules_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp_path, rules_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        revision = hashlib.sha256(rules.encode()).hexdigest()
        return HTMLResponse(_rules_feedback(f"Rules saved. Revision: {revision}"))

    @app.get("/watches/{watch_id}/dry-run", response_class=HTMLResponse, response_model=None)
    def dry_run(watch_id: str, item: Path) -> str | JSONResponse:
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(status_code=404, content={"detail": "watch folder not configured"})
        plan = processor.plan(PlanRequest(watch_id, config.watch_root, item, config.rules_path, config.boundary_policy))
        report = processor.execute(plan, ExecutionMode.DRY_RUN)
        rows = "".join(
            f"<li>{plan.rule_name}: {action.kind} {plan.source} to {action.target}</li>" for action in report.actions
        )
        return f"<h1>Dry run: {watch_id}</h1><ul>{rows}</ul>"

    @app.put("/watches/{watch_id}/rules", response_model=None)
    def save_rules(
        watch_id: str, expected_revision: str, body: bytes = Body(...)
    ) -> dict[str, str] | JSONResponse:
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(status_code=404, content={"detail": "watch folder not configured"})
        rules_path = config.rules_path
        try:
            processor._resolve_destination(rules_path)
        except ValueError as error:
            return JSONResponse(status_code=500, content={"detail": f"unsafe rules path: {error}"})
        loaded = yaml.safe_load(body) or {}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("rules", []), list):
            return JSONResponse(status_code=422, content={"detail": "invalid rules document"})
        temp_path = rules_path.with_suffix(f"{rules_path.suffix}.tmp-{uuid.uuid4().hex}")
        try:
            temp_path.write_bytes(body)
            diagnostics = processor.validate_rules_document(temp_path)
            if diagnostics:
                return JSONResponse(status_code=422, content={"detail": "; ".join(diagnostics)})
            try:
                current_revision = hashlib.sha256(rules_path.read_bytes()).hexdigest()
            except OSError as error:
                return JSONResponse(status_code=500, content={"detail": f"cannot read current rules: {error}"})
            if current_revision != expected_revision:
                return JSONResponse(
                    status_code=409,
                    content={"detail": "ruleset revision conflict", "revision": current_revision},
                )
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, rules_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return {"watch_id": watch_id, "revision": hashlib.sha256(body).hexdigest()}

    @app.get("/attempts", response_model=None)
    def list_attempts(request: Request, status: str = "", watch_id: str = "") -> list[dict[str, object]] | HTMLResponse | JSONResponse:
        if watch_id and _watch_config(watch_id) is None:
            return JSONResponse(status_code=404, content={"detail": "watch folder not configured"})
        statuses = tuple(s.strip() for s in status.split(",") if s.strip()) if status else ()
        summaries = review.list(AttemptFilters(statuses=statuses, watch_id=watch_id))
        if _is_html_request(request):
            return _fragment(request, "attempt_list.html", attempts=summaries, status=status, watch_id=watch_id, watches=watch_folders or ())
        return [
            {
                "attempt_id": s.attempt_id,
                "watch_id": s.watch_id,
                "source_path": s.source_path,
                "source_fingerprint": s.source_fingerprint,
                "rule_name": s.rule_name,
                "status": s.status,
                "failure_detail": s.failure_detail,
                "created_at": s.created_at,
                "retry_of_attempt_id": s.retry_of_attempt_id,
            }
            for s in summaries
        ]

    @app.get("/attempts/{attempt_id}", response_model=None)
    def inspect_attempt(request: Request, attempt_id: str) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            details = review.inspect(attempt_id)
        except ValueError as error:
            return JSONResponse(status_code=404, content={"detail": str(error)})
        if _is_html_request(request):
            config = _watch_config(details.watch_id)
            return _fragment(request, "attempt_detail.html", attempt=details, watch=config)
        return _attempt_payload(details)

    @app.post("/attempts/{attempt_id}/accept", response_model=None)
    def accept_attempt(request: Request, attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            action_index = int(str(payload["action_index"]))
            resulting_path = str(payload["resulting_path"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            details = review.inspect(attempt_id)
        except ValueError as error:
            return JSONResponse(status_code=404, content={"detail": str(error)})
        config = _watch_config(details.watch_id)
        if config is None:
            return JSONResponse(status_code=404, content={"detail": "watch folder not configured"})
        try:
            result = review.command(
                attempt_id,
                Accept(
                    action_index=action_index,
                    resulting_path=resulting_path,
                    watch_root=config.watch_root,
                    boundary_policy=config.boundary_policy,
                ),
            )
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(request, "command_feedback.html", result=result, attempt_id=attempt_id)
        return {"success": result.success, "attempt_id": result.attempt_id, "status": result.status, "detail": result.detail}

    @app.post("/attempts/{attempt_id}/abandon", response_model=None)
    def abandon_attempt(request: Request, attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            reason = str(payload.get("reason", ""))
        except (UnicodeDecodeError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            result = review.command(attempt_id, Abandon(reason=reason))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(request, "command_feedback.html", result=result, attempt_id=attempt_id)
        return {"success": result.success, "attempt_id": result.attempt_id, "status": result.status, "detail": result.detail}

    @app.post("/attempts/{attempt_id}/reopen", response_model=None)
    def reopen_attempt(request: Request, attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            watch_id = str(payload["watch_id"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(status_code=404, content={"detail": "watch folder not configured"})
        try:
            result = review.command(attempt_id, Reopen(watch_root=config.watch_root, rules_path=config.rules_path, boundary_policy=config.boundary_policy))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(request, "command_feedback.html", result=result, attempt_id=attempt_id)
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
            "new_attempt_id": result.new_attempt_id,
        }

    @app.post("/attempts/{attempt_id}/retry", response_model=None)
    def retry_attempt(request: Request, attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | HTMLResponse | JSONResponse:
        try:
            payload = _form_or_json(body)
            watch_id = str(payload["watch_id"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        config = _watch_config(watch_id)
        if config is None:
            return JSONResponse(status_code=404, content={"detail": "watch folder not configured"})
        try:
            result = review.command(attempt_id, RetryFromStart(watch_root=config.watch_root, rules_path=config.rules_path, boundary_policy=config.boundary_policy))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        if _is_html_request(request):
            return _fragment(request, "command_feedback.html", result=result, attempt_id=attempt_id)
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
            "new_attempt_id": result.new_attempt_id,
        }

    @app.get("/logs", response_model=None)
    def get_logs(
        request: Request,
        watch: str = "",
        level: str = "",
        limit: int = 1000,
        start: str = "",
        end: str = "",
    ) -> list[dict[str, object]] | HTMLResponse:
        if log_sink is None:
            return []
        try:
            log_level = LogLevel(level) if level else None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid log level")
        entries = log_sink.read_recent(limit=None, watch=watch, level=log_level)
        if start:
            entries = [entry for entry in entries if entry.timestamp[:10] >= start]
        if end:
            entries = [entry for entry in entries if entry.timestamp[:10] <= end]
        entries = entries[-limit:]
        payload: list[dict[str, object]] = [
            {
                "timestamp": entry.timestamp,
                "level": entry.level.value,
                "watch": entry.watch,
                "rule": entry.rule,
                "action": entry.action,
                "item": entry.item,
                "result": entry.result.value,
                "detail": entry.detail,
            }
            for entry in entries
        ]
        if _is_html_request(request):
            return _fragment(request, "log_viewer.html", entries=payload, watch=watch, level=level, start=start, end=end, watches=watch_folders or ())
        return payload

    @app.get("/health", response_model=None)
    def get_health() -> dict[str, object]:
        if health_checker is None or db_path is None or watch_folders is None:
            return {
                "all_healthy": True,
                "watch_folder_healths": [],
                "persistence_health": {"tracking_db_writable": True, "detail": ""},
            }
        folder_tuples = [(config.watch_id, config.watch_root) for config in watch_folders]
        overall = health_checker.check_all(watch_folders=folder_tuples, db_path=db_path)
        return {
            "all_healthy": overall.all_healthy,
            "watch_folder_healths": [
                {
                    "watch_id": h.watch_id,
                    "accessible": h.accessible,
                    "detail": h.detail,
                }
                for h in overall.watch_folder_healths
            ],
            "persistence_health": {
                "tracking_db_writable": overall.persistence_health.tracking_db_writable,
                "detail": overall.persistence_health.detail,
            },
        }

    return app
