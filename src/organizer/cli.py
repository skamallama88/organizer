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
from organizer.config import OrganizerConfig, WatchFolderConfig, load_config
from organizer.daemon import create_daemon
from organizer.item_processor import ItemProcessor, ItemSnapshot, PlanRequest

app = typer.Typer(no_args_is_help=True)
review_app = typer.Typer(no_args_is_help=True)
app.add_typer(review_app, name="review")


@app.callback()
def main() -> None:
    """Organizer command-line interface."""


@app.command()
def check(
    watch_id: str,
    item: Path,
    config_path: Path = Path("/config/organizer.yaml"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Render an immutable dry-run plan without mutating the item or attempts."""
    config = load_config(config_path)
    watch = _watch(config, watch_id)
    processor = ItemProcessor(attempts_path=attempts_path)
    try:
        stat = item.stat()
    except OSError as error:
        typer.echo(f"cannot stat item: {error}")
        raise typer.Exit(code=1) from error
    snapshot = ItemSnapshot(path=item, size=stat.st_size, mtime=stat.st_mtime)
    batch = processor.process_batch(
        watch_id=watch_id,
        watch_root=watch.watch_root,
        rules_path=watch.rules_path,
        snapshots=[snapshot],
        stability_interval=0.0,
        boundary_policy=watch.boundary_policy,
        dry_run=True,
    )
    for batch_item in batch.items:
        if batch_item.report:
            for action in batch_item.report.actions:
                typer.echo(f"{action.kind}: {action.source} -> {action.target}")
        else:
            typer.echo(f"{batch_item.status.value}: {batch_item.source}: {batch_item.detail}")


@app.command()
def run(
    config_path: Path = Path("/config/organizer.yaml"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Start Organizer's web server, filesystem watcher, and periodic scanner."""
    config = load_config(config_path)
    processor = ItemProcessor(attempts_path=attempts_path)
    daemon = create_daemon(config, processor)
    daemon.start()
    try:
        import uvicorn
        from organizer.web import create_app

        uvicorn.run(create_app(processor, watch_folders=config.watches, db_path=attempts_path), host="127.0.0.1", port=8000)
    finally:
        daemon.stop()


@review_app.command("list")
def review_list(
    status: str = typer.Option("", help="Filter by status (comma-separated)"),
    watch_id: str = typer.Option("", help="Filter by watch ID"),
    config_path: Path = Path("/config/organizer.yaml"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """List attempts needing review."""
    config = load_config(config_path)
    if watch_id:
        _watch(config, watch_id)
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    statuses = tuple(s.strip() for s in status.split(",") if s.strip()) if status else ()
    summaries = review.list(AttemptFilters(statuses=statuses, watch_id=watch_id))
    for s in summaries:
        typer.echo(f"{s.attempt_id}  {s.status:22s}  {s.watch_id:12s}  {s.rule_name:20s}  {s.source_path}")


@review_app.command("inspect")
def review_inspect(
    attempt_id: str,
    config_path: Path = Path("/config/organizer.yaml"),
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
    watch_id: str,
    config_path: Path = Path("/config/organizer.yaml"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Accept an uncertain action result during reconciliation."""
    config = load_config(config_path)
    watch = _watch(config, watch_id)
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    result = review.command(
        attempt_id,
        Accept(action_index=action_index, resulting_path=resulting_path, watch_root=watch.watch_root, boundary_policy=watch.boundary_policy),
    )
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
    watch_id: str,
    config_path: Path = Path("/config/organizer.yaml"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Reopen an abandoned attempt with a fresh plan."""
    watch = _watch(load_config(config_path), watch_id)
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    result = review.command(attempt_id, Reopen(watch_root=watch.watch_root, rules_path=watch.rules_path, boundary_policy=watch.boundary_policy))
    typer.echo(f"Reopened: {result.detail}")
    if result.new_attempt_id:
        typer.echo(f"New attempt: {result.new_attempt_id}")


@review_app.command("retry")
def review_retry(
    attempt_id: str,
    watch_id: str,
    config_path: Path = Path("/config/organizer.yaml"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Retry an attempt from the start with a fresh plan."""
    watch = _watch(load_config(config_path), watch_id)
    processor = ItemProcessor(attempts_path=attempts_path)
    review = AttemptReview(processor)
    result = review.command(attempt_id, RetryFromStart(watch_root=watch.watch_root, rules_path=watch.rules_path, boundary_policy=watch.boundary_policy))
    typer.echo(f"Retried: {result.detail}")
    if result.new_attempt_id:
        typer.echo(f"New attempt: {result.new_attempt_id}")


@app.command()
def status(
    config_path: Path = Path("/config/organizer.yaml"),
    attempts_path: Path = Path("/config/organizer.db"),
) -> None:
    """Show configured watches and their current health."""
    config = load_config(config_path)
    processor = ItemProcessor(attempts_path=attempts_path)
    for watch in config.watches:
        rules = processor.validate_rules_document(watch.rules_path) if watch.rules_path.exists() else ["rules file missing"]
        rule_count = _rule_count(watch.rules_path)
        health = "healthy" if watch.watch_root.is_dir() and not rules else "unhealthy"
        last_activity = _last_activity(processor, watch.watch_id)
        typer.echo(f"{watch.watch_id}: {health}  root={watch.watch_root}  rules={watch.rules_path}  rule_count={rule_count}  last_activity={last_activity}")


def _watch(config: OrganizerConfig, watch_id: str) -> WatchFolderConfig:
    try:
        return config.watch(watch_id)
    except KeyError as error:
        raise typer.BadParameter(f"watch folder not configured: {watch_id}") from error


def _rule_count(path: Path) -> int:
    if not path.exists():
        return 0
    import yaml
    loaded = yaml.safe_load(path.read_text()) or {}
    return len(loaded.get("rules", [])) if isinstance(loaded, dict) and isinstance(loaded.get("rules", []), list) else 0


def _last_activity(processor: ItemProcessor, watch_id: str) -> str:
    summaries = AttemptReview(processor).list(AttemptFilters(watch_id=watch_id))
    return summaries[0].created_at if summaries else "never"
