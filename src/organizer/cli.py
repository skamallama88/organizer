from pathlib import Path

import typer

from organizer.attempt_review import (
    Abandon,
    Accept,
    AttemptFilters,
    AttemptReview,
    Reopen,
    RetryFromStart,
)
from organizer.item_processor import ExecutionMode, ItemProcessor, PlanRequest

app = typer.Typer(no_args_is_help=True)
review_app = typer.Typer(no_args_is_help=True)
app.add_typer(review_app, name="review")


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


@review_app.command("list")
def review_list(
    status: str = typer.Option("", help="Filter by status (comma-separated)"),
    watch_id: str = typer.Option("", help="Filter by watch ID"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """List attempts needing review."""
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    statuses = tuple(s.strip() for s in status.split(",") if s.strip()) if status else ()
    summaries = review.list(AttemptFilters(statuses=statuses, watch_id=watch_id))
    for s in summaries:
        typer.echo(f"{s.attempt_id}  {s.status:22s}  {s.watch_id:12s}  {s.rule_name:20s}  {s.source_path}")


@review_app.command("inspect")
def review_inspect(
    attempt_id: str,
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Show full details of an attempt."""
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    details = review.inspect(attempt_id)
    typer.echo(f"Attempt:       {details.attempt_id}")
    typer.echo(f"Watch:         {details.watch_id}")
    typer.echo(f"Source:        {details.source_path}")
    typer.echo(f"Fingerprint:   {details.source_fingerprint}")
    typer.echo(f"Rule:          {details.rule_name}")
    typer.echo(f"Status:        {details.status}")
    typer.echo(f"Failure:       {details.failure_detail}")
    typer.echo(f"Abandoned:     {details.abandoned_reason}")
    typer.echo(f"Lineage:       {', '.join(details.processing_lineage)}")
    typer.echo(f"Linked:        {', '.join(details.linked_attempts)}")
    typer.echo("Planned actions:")
    for action in details.planned_actions:
        typer.echo(f"  {action['kind']} -> {action['target']}")
    typer.echo("Action results:")
    for result in details.action_results:
        typer.echo(f"  {result['kind']} {result['result']} {result.get('detail', '')}")
    if details.accepted_results:
        typer.echo("Accepted results:")
        for accepted in details.accepted_results:
            typer.echo(f"  action {accepted['action_index']} -> {accepted['resulting_path']}")
    if details.suppressions:
        typer.echo("Suppressions:")
        for sup in details.suppressions:
            typer.echo(f"  {sup['reason']} at {sup['suppressed_at']}")


@review_app.command("accept")
def review_accept(
    attempt_id: str,
    action_index: int,
    resulting_path: str,
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Accept an uncertain action result during reconciliation."""
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    result = review.command(attempt_id, Accept(action_index=action_index, resulting_path=resulting_path))
    typer.echo(f"Accepted: {result.detail}")


@review_app.command("abandon")
def review_abandon(
    attempt_id: str,
    reason: str,
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Abandon an attempt and suppress automatic retries."""
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    result = review.command(attempt_id, Abandon(reason=reason))
    typer.echo(f"Abandoned: {result.detail}")


@review_app.command("reopen")
def review_reopen(
    attempt_id: str,
    watch_root: Path,
    rules_path: Path,
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Reopen an abandoned attempt with a fresh plan."""
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    result = review.command(attempt_id, Reopen(watch_root=watch_root, rules_path=rules_path))
    typer.echo(f"Reopened: {result.detail}")
    if result.new_attempt_id:
        typer.echo(f"New attempt: {result.new_attempt_id}")


@review_app.command("retry")
def review_retry(
    attempt_id: str,
    watch_root: Path,
    rules_path: Path,
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Retry an attempt from the start with a fresh plan."""
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    result = review.command(attempt_id, RetryFromStart(watch_root=watch_root, rules_path=rules_path))
    typer.echo(f"Retried: {result.detail}")
    if result.new_attempt_id:
        typer.echo(f"New attempt: {result.new_attempt_id}")
