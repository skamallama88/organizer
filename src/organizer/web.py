from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

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

    return app
