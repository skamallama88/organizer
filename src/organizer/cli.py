from pathlib import Path

import typer

from organizer.item_processor import ExecutionMode, ItemProcessor, PlanRequest

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Organizer command-line interface."""


@app.command()
def check(
    watch_id: str,
    watch_root: Path,
    item: Path,
    rules_path: Path,
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Render an immutable dry-run plan without mutating the item or attempts."""
    processor = ItemProcessor(attempts_path=attempts_path)
    plan = processor.plan(PlanRequest(watch_id, watch_root, item, rules_path))
    report = processor.execute(plan, ExecutionMode.DRY_RUN)
    for result in report.actions:
        typer.echo(f"{plan.rule_name}: {result.kind} {plan.source} -> {result.target}")
