from pathlib import Path
import hashlib
import yaml

from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from organizer.item_processor import ExecutionMode, ItemProcessor, PlanRequest


def create_app(processor: ItemProcessor) -> FastAPI:
    app = FastAPI()

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

    return app
