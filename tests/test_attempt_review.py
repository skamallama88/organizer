from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from organizer.attempt_review import (
    Abandon,
    Accept,
    AttemptFilters,
    AttemptReview,
    AttemptSummary,
    Reopen,
    RetryFromStart,
)
from organizer.item_processor import (
    ItemProcessor,
    ItemSnapshot,
)


def write_move_rules(path: Path, destination: str) -> Path:
    path.write_text(
        f"""rules:
  - name: move
    match:
      field: file_name
      pattern: '.*'
    actions:
      - move:
          destination: {destination}
"""
    )
    return path


def write_delete_direct_rules(path: Path) -> Path:
    path.write_text(
        """rules:
  - name: delete
    match:
      field: file_name
      pattern: '.*'
    actions:
      - delete:
          mode: direct
    allow_direct_deletion: true
"""
    )
    return path


def _create_needs_reconciliation_attempt(tmp_path: Path) -> tuple[ItemProcessor, str, Path, Path, Path]:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_delete_direct_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(
        __import__("organizer.item_processor", fromlist=["PlanRequest"]).PlanRequest(
            watch_id="downloads",
            watch_root=watch_root,
            item=item,
            rules_path=rules,
        )
    )
    item.write_text("changed")
    report = processor.execute(plan)
    assert report.status == "needs-reconciliation"
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute(
            "SELECT attempt_id FROM processing_attempts WHERE status = ?",
            ("needs-reconciliation",),
        ).fetchone()
    return processor, row[0], watch_root, rules, item


def _create_failed_collision_attempt(tmp_path: Path) -> tuple[ItemProcessor, str, Path, Path]:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    (destination / "movie.mkv").write_text("existing")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    snapshot = ItemSnapshot(path=item, size=item.stat().st_size, mtime=item.stat().st_mtime)
    processor.process_batch(
        "downloads", watch_root, rules, [snapshot], stability_interval=0.0, now=1000.0,
    )
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute(
            "SELECT attempt_id FROM processing_attempts WHERE status = ?",
            ("failed",),
        ).fetchone()
    return processor, row[0], watch_root, rules


def test_list_returns_summaries_for_reviewable_attempts(tmp_path: Path) -> None:
    processor, attempt_id, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    summaries = review.list(AttemptFilters(statuses=("needs-reconciliation",)))

    assert len(summaries) == 1
    assert isinstance(summaries[0], AttemptSummary)
    assert summaries[0].attempt_id == attempt_id
    assert summaries[0].status == "needs-reconciliation"
    assert summaries[0].watch_id == "downloads"
    assert summaries[0].rule_name == "delete"
    assert summaries[0].source_fingerprint != ""


def test_list_filters_by_watch_id(tmp_path: Path) -> None:
    processor, attempt_id, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    summaries = review.list(AttemptFilters(watch_id="downloads"))
    assert len(summaries) == 1
    assert summaries[0].attempt_id == attempt_id

    summaries = review.list(AttemptFilters(watch_id="other-watch"))
    assert len(summaries) == 0


def test_list_filters_by_multiple_statuses(tmp_path: Path) -> None:
    processor, _, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    summaries = review.list(AttemptFilters(statuses=("needs-reconciliation", "failed")))
    assert len(summaries) == 1
    assert summaries[0].status == "needs-reconciliation"


def test_list_returns_empty_when_no_matches(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")
    review = AttemptReview(processor)

    summaries = review.list(AttemptFilters(statuses=("needs-reconciliation",)))
    assert summaries == []


def test_inspect_returns_full_details(tmp_path: Path) -> None:
    processor, attempt_id, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    details = review.inspect(attempt_id)

    assert details.attempt_id == attempt_id
    assert details.watch_id == "downloads"
    assert details.rule_name == "delete"
    assert details.status == "needs-reconciliation"
    assert details.source_fingerprint != ""
    assert details.source_size > 0
    assert details.created_at != ""
    assert len(details.planned_actions) > 0
    assert len(details.action_results) > 0
    assert details.action_results[0]["result"] == "UNCERTAIN"


def test_inspect_shows_suppressions_for_attempt(tmp_path: Path) -> None:
    processor, attempt_id, _, _ = _create_failed_collision_attempt(tmp_path)
    review = AttemptReview(processor)

    details = review.inspect(attempt_id)

    assert len(details.suppressions) > 0
    assert details.suppressions[0]["reason"] == "collision"


def test_inspect_shows_linked_attempts(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, rules = _create_failed_collision_attempt(tmp_path)

    (watch_root.parent / "videos" / "movie.mkv").unlink()
    processor.retry_attempt(attempt_id, watch_root, rules)

    review = AttemptReview(processor)
    details = review.inspect(attempt_id)

    assert len(details.linked_attempts) > 0


def test_inspect_raises_for_unknown_attempt(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="attempt not found"):
        review.inspect("nonexistent-id")


def test_accept_creates_immutable_accepted_result(tmp_path: Path) -> None:
    processor, attempt_id, _, _, item = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)
    resulting_path = str(item)

    result = review.command(attempt_id, Accept(action_index=0, resulting_path=resulting_path))

    assert result.success is True
    assert result.attempt_id == attempt_id
    details = review.inspect(attempt_id)
    assert len(details.accepted_results) == 1
    assert details.accepted_results[0]["action_index"] == 0
    assert details.accepted_results[0]["resulting_path"] == resulting_path


def test_accept_is_immutable(tmp_path: Path) -> None:
    processor, attempt_id, _, _, item = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)
    resulting_path = str(item)

    review.command(attempt_id, Accept(action_index=0, resulting_path=resulting_path))

    with pytest.raises(ValueError, match="already accepted"):
        review.command(attempt_id, Accept(action_index=0, resulting_path="/different/path"))


def test_accept_rejects_for_non_reconciling_attempt(tmp_path: Path) -> None:
    processor, attempt_id, _, _ = _create_failed_collision_attempt(tmp_path)
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="needs-reconciliation"):
        review.command(attempt_id, Accept(action_index=0, resulting_path="/some/path"))


def test_accept_rejects_invalid_action_index(tmp_path: Path) -> None:
    processor, attempt_id, _, _, item = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="action index"):
        review.command(attempt_id, Accept(action_index=99, resulting_path=str(item)))


def test_abandon_creates_terminal_state_and_suppression(tmp_path: Path) -> None:
    processor, attempt_id, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    result = review.command(attempt_id, Abandon(reason="unresolvable"))

    assert result.success is True
    assert result.status == "abandoned"
    details = review.inspect(attempt_id)
    assert details.status == "abandoned"
    assert details.abandoned_reason == "unresolvable"


def test_abandon_creates_suppression(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, item = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)
    details = review.inspect(attempt_id)

    review.command(attempt_id, Abandon(reason="giving up"))

    assert processor.has_suppressed_attempt("downloads", item, details.source_fingerprint) is True


def test_abandon_rejects_completed_attempt(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    (tmp_path / "videos").mkdir()
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(
        __import__("organizer.item_processor", fromlist=["PlanRequest"]).PlanRequest(
            watch_id="downloads", watch_root=watch_root, item=item, rules_path=rules,
        )
    )
    processor.execute(plan)
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute(
            "SELECT attempt_id FROM processing_attempts WHERE status = ?",
            ("completed",),
        ).fetchone()
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="cannot be abandoned"):
        review.command(row[0], Abandon(reason="test"))


def test_reopen_creates_fresh_plan_and_clears_suppression(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, item = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)
    details = review.inspect(attempt_id)

    review.command(attempt_id, Abandon(reason="test"))
    assert processor.has_suppressed_attempt("downloads", item, details.source_fingerprint) is True

    result = review.command(attempt_id, Reopen(watch_root=watch_root, rules_path=watch_root / "rules.yaml"))

    assert result.success is True
    assert result.new_attempt_id is not None
    assert result.new_attempt_id != attempt_id
    assert processor.has_suppressed_attempt("downloads", item, details.source_fingerprint) is False


def test_reopen_preserves_abandoned_history(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    review.command(attempt_id, Abandon(reason="test"))
    result = review.command(attempt_id, Reopen(watch_root=watch_root, rules_path=watch_root / "rules.yaml"))

    old_details = review.inspect(attempt_id)
    assert old_details.status == "abandoned"
    assert result.new_attempt_id is not None
    assert result.new_attempt_id != attempt_id

    new_details = review.inspect(result.new_attempt_id)
    assert new_details.status in ("completed", "failed", "needs-reconciliation", "started")


def test_reopen_rejects_non_abandoned_attempt(tmp_path: Path) -> None:
    processor, attempt_id, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="abandoned"):
        review.command(attempt_id, Reopen(watch_root=tmp_path, rules_path=tmp_path / "rules.yaml"))


def test_retry_from_start_creates_linked_attempt(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, rules = _create_failed_collision_attempt(tmp_path)
    review = AttemptReview(processor)

    (watch_root.parent / "videos" / "movie.mkv").unlink()
    result = review.command(
        attempt_id,
        RetryFromStart(watch_root=watch_root, rules_path=rules),
    )

    assert result.success is True
    assert result.new_attempt_id is not None
    assert result.new_attempt_id != attempt_id
    details = review.inspect(attempt_id)
    assert attempt_id in details.linked_attempts or len(details.linked_attempts) > 0


def test_retry_from_start_clears_suppression(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, rules = _create_failed_collision_attempt(tmp_path)
    review = AttemptReview(processor)
    item = watch_root / "movie.mkv"
    fingerprint = processor._fingerprint(item)
    assert processor.has_suppressed_attempt("downloads", item, fingerprint) is True

    (watch_root.parent / "videos" / "movie.mkv").unlink()
    review.command(attempt_id, RetryFromStart(watch_root=watch_root, rules_path=rules))

    assert processor.has_suppressed_attempt("downloads", item, fingerprint) is False


def test_retry_from_start_rejects_completed_attempt(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    (tmp_path / "videos").mkdir()
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(
        __import__("organizer.item_processor", fromlist=["PlanRequest"]).PlanRequest(
            watch_id="downloads", watch_root=watch_root, item=item, rules_path=rules,
        )
    )
    processor.execute(plan)
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute(
            "SELECT attempt_id FROM processing_attempts WHERE status = ?",
            ("completed",),
        ).fetchone()
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="not retryable"):
        review.command(row[0], RetryFromStart(watch_root=watch_root, rules_path=rules))


def test_list_includes_abandoned_attempts(tmp_path: Path) -> None:
    processor, attempt_id, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)
    review.command(attempt_id, Abandon(reason="test"))

    summaries = review.list(AttemptFilters(statuses=("abandoned",)))

    assert len(summaries) == 1
    assert summaries[0].attempt_id == attempt_id
    assert summaries[0].status == "abandoned"


def test_inspect_shows_processing_lineage(tmp_path: Path) -> None:
    processor, attempt_id, _, _, _ = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    details = review.inspect(attempt_id)

    assert isinstance(details.processing_lineage, tuple)
    assert "downloads" in details.processing_lineage


def test_command_result_includes_detail(tmp_path: Path) -> None:
    processor, attempt_id, _, _, item = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)

    result = review.command(attempt_id, Accept(action_index=0, resulting_path=str(item)))

    assert result.success is True
    assert result.detail != ""
