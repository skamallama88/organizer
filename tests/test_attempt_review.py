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
    MarkActionApplied,
    Reopen,
    RetryRemaining,
    RetryFromStart,
)
from organizer.item_processor import (
    ItemProcessor,
    ItemSnapshot,
    iso_timestamp,
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


def write_archive_rules(path: Path, destination: str, preserve_original: bool = True) -> Path:
    path.write_text(
        f"""rules:
  - name: archive
    match:
      field: file_name
      pattern: '.*'
    actions:
      - archive:
          destination: {destination}
          extension: .zip
          preserve_originals: {str(preserve_original).lower()}
"""
    )
    return path


def write_uncertain_then_move_rules(path: Path, destination: str) -> Path:
    path.write_text(
        f"""rules:
  - name: archive then move
    match:
      field: file_name
      pattern: '.*'
    actions:
      - archive:
          destination: {destination}
          extension: .zip
          preserve_originals: false
      - move:
          destination: {destination}
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


def _create_uncertain_archive_attempt(tmp_path: Path) -> tuple[ItemProcessor, str, Path, Path, Path]:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "archives"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_archive_rules(watch_root / "rules.yaml", "../archives", preserve_original=False)
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(
        __import__("organizer.item_processor", fromlist=["PlanRequest"]).PlanRequest(
            watch_id="downloads",
            watch_root=watch_root,
            item=item,
            rules_path=rules,
        )
    )
    original_publish = processor._publish_staged

    def publish_then_change(staging: Path, target: Path) -> None:
        original_publish(staging, target)
        item.write_text("changed")

    processor._publish_staged = publish_then_change  # type: ignore[method-assign]
    report = processor.execute(plan)
    assert report.status == "needs-reconciliation"
    archive_path = destination / "movie.mkv.zip"
    assert archive_path.exists()
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute(
            "SELECT attempt_id FROM processing_attempts WHERE status = ?",
            ("needs-reconciliation",),
        ).fetchone()
    return processor, row[0], watch_root, rules, archive_path


def _create_uncertain_continuation_attempt(tmp_path: Path) -> tuple[ItemProcessor, str, Path, Path, Path]:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "archives"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_uncertain_then_move_rules(watch_root / "rules.yaml", "../archives")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(
        __import__("organizer.item_processor", fromlist=["PlanRequest"]).PlanRequest(
            watch_id="downloads", watch_root=watch_root, item=item, rules_path=rules,
        )
    )
    original_publish = processor._publish_staged

    def publish_then_change(staging: Path, target: Path) -> None:
        original_publish(staging, target)
        item.write_text("changed")

    processor._publish_staged = publish_then_change  # type: ignore[method-assign]
    report = processor.execute(plan)
    assert report.status == "needs-reconciliation"
    archive_path = destination / "movie.mkv.zip"
    assert archive_path.exists()
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute(
            "SELECT attempt_id FROM processing_attempts WHERE status = ?",
            ("needs-reconciliation",),
        ).fetchone()
    return processor, row[0], watch_root, rules, archive_path


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


def test_iso_timestamp_converts_epoch_string_to_iso(tmp_path: Path) -> None:
    assert iso_timestamp("") == ""
    assert iso_timestamp("1786471480.84").endswith("+00:00")
    assert "T" in iso_timestamp("1786471480.84")
    assert iso_timestamp("already-iso") == "already-iso"
    assert iso_timestamp("not-a-number") == "not-a-number"


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
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)
    resulting_path = str(archive_path)

    result = review.command(
        attempt_id,
        Accept(action_index=0, resulting_path=resulting_path, watch_root=watch_root),
    )

    assert result.success is True
    assert result.attempt_id == attempt_id
    details = review.inspect(attempt_id)
    assert len(details.accepted_results) == 1
    assert details.accepted_results[0]["action_index"] == 0
    assert details.accepted_results[0]["resulting_path"] == resulting_path
    assert details.accepted_results[0].get("fingerprint", "") != ""


def test_mark_action_applied_records_audit_and_requires_evidence(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)

    result = review.command(
        attempt_id,
        MarkActionApplied(action_index=0, resulting_path=str(archive_path), watch_root=watch_root),
    )

    assert result.success is True
    details = review.inspect(attempt_id)
    assert details.accepted_results[0]["action_index"] == 0
    assert details.accepted_results[0]["command"] == "mark-action-applied"
    assert any(event["command"] == "mark-action-applied" for event in details.audit_events)


def test_retry_remaining_executes_only_historical_suffix_and_links_attempt(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, rules, archive_path = _create_uncertain_continuation_attempt(tmp_path)
    review = AttemptReview(processor)
    review.command(
        attempt_id,
        Accept(action_index=0, resulting_path=str(archive_path), watch_root=watch_root),
    )

    result = review.command(
        attempt_id,
        RetryRemaining(action_index=0, resulting_path=str(archive_path), watch_root=watch_root),
    )

    assert result.success is True
    assert result.new_attempt_id is not None
    assert (tmp_path / "archives" / "movie.mkv.zip").exists() is True
    assert (tmp_path / "archives" / "movie.mkv.zip" / "movie.mkv.zip").exists() is False
    new_details = review.inspect(result.new_attempt_id)
    assert [action["kind"] for action in new_details.planned_actions] == ["move"]
    assert new_details.source_path == str(archive_path)
    assert new_details.retry_of_attempt_id == attempt_id
    assert review.inspect(attempt_id).status == "needs-reconciliation"


def test_retry_remaining_rejects_changed_accepted_identity(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_continuation_attempt(tmp_path)
    review = AttemptReview(processor)
    review.command(attempt_id, Accept(action_index=0, resulting_path=str(archive_path), watch_root=watch_root))
    archive_path.write_text("changed")

    with pytest.raises(ValueError, match="identity"):
        review.command(
            attempt_id,
            RetryRemaining(action_index=0, resulting_path=str(archive_path), watch_root=watch_root),
        )


def test_retry_remaining_rejects_when_no_actions_remain(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)
    review.command(attempt_id, Accept(action_index=0, resulting_path=str(archive_path), watch_root=watch_root))

    with pytest.raises(ValueError, match="no remaining actions"):
        review.command(
            attempt_id,
            RetryRemaining(action_index=0, resulting_path=str(archive_path), watch_root=watch_root),
        )


def test_accept_is_immutable(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)
    resulting_path = str(archive_path)

    review.command(
        attempt_id,
        Accept(action_index=0, resulting_path=resulting_path, watch_root=watch_root),
    )

    with pytest.raises(ValueError, match="already accepted"):
        review.command(
            attempt_id,
            Accept(action_index=0, resulting_path="/different/path", watch_root=watch_root),
        )


def test_accept_rejects_for_non_reconciling_attempt(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, rules = _create_failed_collision_attempt(tmp_path)
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="needs-reconciliation"):
        review.command(
            attempt_id,
            Accept(action_index=0, resulting_path="/some/path", watch_root=watch_root),
        )


def test_accept_rejects_invalid_action_index(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)

    with pytest.raises(ValueError, match="action index"):
        review.command(
            attempt_id,
            Accept(action_index=99, resulting_path=str(archive_path), watch_root=watch_root),
        )


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


def test_reopen_preserves_suppression_when_planning_fails(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, item = _create_needs_reconciliation_attempt(tmp_path)
    review = AttemptReview(processor)
    details = review.inspect(attempt_id)
    review.command(attempt_id, Abandon(reason="test"))
    assert processor.has_suppressed_attempt("downloads", item, details.source_fingerprint) is True
    # Remove the source so planning fails
    item.unlink()

    with pytest.raises((ValueError, OSError)):
        review.command(attempt_id, Reopen(watch_root=watch_root, rules_path=watch_root / "rules.yaml"))

    assert processor.has_suppressed_attempt("downloads", item, details.source_fingerprint) is True


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


def test_retry_from_start_preserves_suppression_when_planning_fails(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, rules = _create_failed_collision_attempt(tmp_path)
    review = AttemptReview(processor)
    item = watch_root / "movie.mkv"
    fingerprint = processor._fingerprint(item)
    assert processor.has_suppressed_attempt("downloads", item, fingerprint) is True
    item.unlink()
    (watch_root.parent / "videos" / "movie.mkv").unlink()

    with pytest.raises((ValueError, OSError)):
        review.command(attempt_id, RetryFromStart(watch_root=watch_root, rules_path=rules))

    assert processor.has_suppressed_attempt("downloads", item, fingerprint) is True


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
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)

    result = review.command(
        attempt_id,
        Accept(action_index=0, resulting_path=str(archive_path), watch_root=watch_root),
    )

    assert result.success is True
    assert result.detail != ""


def test_accept_rejects_missing_resulting_path(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, _ = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)
    (tmp_path / "archives" / "movie.mkv.zip").unlink()

    with pytest.raises(ValueError, match="does not exist"):
        review.command(
            attempt_id,
            Accept(
                action_index=0,
                resulting_path=str(tmp_path / "archives" / "movie.mkv.zip"),
                watch_root=watch_root,
            ),
        )


def test_accept_rejects_unexpected_resulting_path(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)
    other = tmp_path / "archives" / "other.zip"
    other.write_text("other")

    with pytest.raises(ValueError, match="unexpected resulting path"):
        review.command(
            attempt_id,
            Accept(action_index=0, resulting_path=str(other), watch_root=watch_root),
        )


def test_accept_rejects_path_outside_data_volumes(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)
    policy = __import__("organizer.item_processor", fromlist=["BoundaryPolicy"]).BoundaryPolicy(
        data_roots=(tmp_path / "downloads",),
    )

    with pytest.raises(ValueError, match="outside data"):
        review.command(
            attempt_id,
            Accept(
                action_index=0,
                resulting_path=str(archive_path),
                watch_root=watch_root,
                boundary_policy=policy,
            ),
        )


def test_accept_rejects_path_outside_allowed_destinations(tmp_path: Path) -> None:
    processor, attempt_id, watch_root, _, archive_path = _create_uncertain_archive_attempt(tmp_path)
    review = AttemptReview(processor)
    policy = __import__("organizer.item_processor", fromlist=["BoundaryPolicy"]).BoundaryPolicy(
        data_roots=(tmp_path,),
        allowed_destinations=(tmp_path / "other",),
    )

    with pytest.raises(ValueError, match="allowed destination"):
        review.command(
            attempt_id,
            Accept(
                action_index=0,
                resulting_path=str(archive_path),
                watch_root=watch_root,
                boundary_policy=policy,
            ),
        )
