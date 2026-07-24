from pathlib import Path
import hashlib
import json
import yaml

from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from organizer.attempt_review import (
    Abandon,
    Accept,
    AttemptFilters,
    AttemptReview,
    Reopen,
    RetryFromStart,
)
from organizer.item_processor import ExecutionMode, ItemProcessor, PlanRequest


def create_app(processor: ItemProcessor) -> FastAPI:
    app = FastAPI()
    review = AttemptReview(processor)

    @app.get("/watches/{watch_id}/dry-run", response_class=HTMLResponse)
    def dry_run(watch_id: str, watch_root: Path, item: Path, rules_path: Path) -> str:
        plan = processor.plan(PlanRequest(watch_id, watch_root, item, rules_path))
        report = processor.execute(plan, ExecutionMode.DRY_RUN)
        rows = "".join(
            f"<li>{plan.rule_name}: {action.kind} {plan.source} to {action.target}</li>" for action in report.actions
        )
        return f"<h1>Dry run: {watch_id}</h1><ul>{rows}</ul>"

    @app.put("/watches/{watch_id}/rules", response_model=None)
    def save_rules(
        watch_id: str, rules_path: Path, expected_revision: str, body: bytes = Body(...)
    ) -> dict[str, str] | JSONResponse:
        loaded = yaml.safe_load(body) or {}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("rules", []), list):
            return JSONResponse(status_code=422, content={"detail": "invalid rules document"})
        current_revision = hashlib.sha256(rules_path.read_bytes()).hexdigest()
        if current_revision != expected_revision:
            return JSONResponse(
                status_code=409,
                content={"detail": "ruleset revision conflict", "revision": current_revision},
            )
        rules_path.write_bytes(body)
        return {"watch_id": watch_id, "revision": hashlib.sha256(body).hexdigest()}

    @app.get("/attempts", response_model=None)
    def list_attempts(status: str = "", watch_id: str = "") -> list[dict[str, object]]:
        statuses = tuple(s.strip() for s in status.split(",") if s.strip()) if status else ()
        summaries = review.list(AttemptFilters(statuses=statuses, watch_id=watch_id))
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
    def inspect_attempt(attempt_id: str) -> dict[str, object] | JSONResponse:
        try:
            details = review.inspect(attempt_id)
        except ValueError as error:
            return JSONResponse(status_code=404, content={"detail": str(error)})
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

    @app.post("/attempts/{attempt_id}/accept", response_model=None)
    def accept_attempt(attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | JSONResponse:
        try:
            payload = json.loads(body)
            action_index = int(payload["action_index"])
            resulting_path = str(payload["resulting_path"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            result = review.command(attempt_id, Accept(action_index=action_index, resulting_path=resulting_path))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        return {"success": result.success, "attempt_id": result.attempt_id, "status": result.status, "detail": result.detail}

    @app.post("/attempts/{attempt_id}/abandon", response_model=None)
    def abandon_attempt(attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | JSONResponse:
        try:
            payload = json.loads(body)
            reason = str(payload.get("reason", ""))
        except json.JSONDecodeError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            result = review.command(attempt_id, Abandon(reason=reason))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        return {"success": result.success, "attempt_id": result.attempt_id, "status": result.status, "detail": result.detail}

    @app.post("/attempts/{attempt_id}/reopen", response_model=None)
    def reopen_attempt(attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | JSONResponse:
        try:
            payload = json.loads(body)
            watch_root = Path(payload["watch_root"])
            rules_path = Path(payload["rules_path"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            result = review.command(attempt_id, Reopen(watch_root=watch_root, rules_path=rules_path))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
            "new_attempt_id": result.new_attempt_id,
        }

    @app.post("/attempts/{attempt_id}/retry", response_model=None)
    def retry_attempt(attempt_id: str, body: bytes = Body(...)) -> dict[str, object] | JSONResponse:
        try:
            payload = json.loads(body)
            watch_root = Path(payload["watch_root"])
            rules_path = Path(payload["rules_path"])
        except (json.JSONDecodeError, KeyError, ValueError) as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        try:
            result = review.command(attempt_id, RetryFromStart(watch_root=watch_root, rules_path=rules_path))
        except ValueError as error:
            return JSONResponse(status_code=422, content={"detail": str(error)})
        return {
            "success": result.success,
            "attempt_id": result.attempt_id,
            "status": result.status,
            "detail": result.detail,
            "new_attempt_id": result.new_attempt_id,
        }

    return app
