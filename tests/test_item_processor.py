from pathlib import Path
import hashlib
import sqlite3
import zipfile
import py7zr
import rarfile

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from organizer.cli import app
from organizer.web import create_app
import pytest

from organizer.item_processor import (
    BoundaryPolicy,
    ExecutionMode,
    ItemProcessor,
    ItemSnapshot,
    PlanRequest,
)


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


def write_quarantine_rules(path: Path) -> Path:
    path.write_text(
        """rules:
  - name: quarantine
    match:
      field: file_name
      pattern: '.*'
    actions:
      - delete:
          mode: quarantine
"""
    )
    return path


def make_request(
    watch_root: Path,
    item: Path,
    rules: Path,
    *,
    policy: BoundaryPolicy | None = None,
) -> PlanRequest:
    return PlanRequest(
        watch_id="downloads",
        watch_root=watch_root,
        item=item,
        rules_path=rules,
        boundary_policy=policy,
    )


def test_delete_rejects_empty_mode(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: delete
    match:
      field: file_name
      pattern: '.*'
    actions:
      - delete: {}
"""
    )

    with pytest.raises(ValueError, match="mode"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def test_delete_direct_requires_opt_in(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: delete
    match:
      field: file_name
      pattern: '.*'
    actions:
      - delete:
          mode: direct
"""
    )

    with pytest.raises(ValueError, match="direct deletion"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def test_delete_quarantine_requires_quarantine_root(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_quarantine_rules(watch_root / "rules.yaml")

    with pytest.raises(ValueError, match="quarantine"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def test_delete_direct_executes_with_opt_in(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_delete_direct_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    assert report.status == "completed"
    assert not item.exists()


def test_quarantine_moves_item_to_quarantine_root(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    quarantine_root = tmp_path / "quarantine"
    watch_root.mkdir()
    quarantine_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_quarantine_rules(watch_root / "rules.yaml")
    policy = BoundaryPolicy(quarantine_root=quarantine_root)
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules, policy=policy)))

    assert report.status == "completed"
    assert not item.exists()
    attempts = processor.attempts()
    resulting_paths = attempts[0]["resulting_paths"]
    assert len(resulting_paths) == 1
    quarantined = Path(resulting_paths[0])
    assert str(quarantine_root) in str(quarantined)
    assert quarantined.name == "movie.mkv"
    assert quarantined.read_text() == "movie"


def test_quarantine_preserves_relative_path(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    quarantine_root = tmp_path / "quarantine"
    (watch_root / "subdir").mkdir(parents=True)
    quarantine_root.mkdir()
    item = watch_root / "subdir" / "doc.pdf"
    item.write_text("pdf")
    rules = write_quarantine_rules(watch_root / "rules.yaml")
    policy = BoundaryPolicy(quarantine_root=quarantine_root)
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules, policy=policy)))

    assert report.status == "completed"
    assert not item.exists()
    attempts = processor.attempts()
    resulting_paths = attempts[0]["resulting_paths"]
    quarantined = Path(resulting_paths[0])
    assert "subdir" in quarantined.parts
    assert quarantined.name == "doc.pdf"


def test_quarantine_with_prior_rename_preserves_original_path(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    quarantine_root = tmp_path / "quarantine"
    watch_root.mkdir()
    quarantine_root.mkdir()
    item = watch_root / "old_name.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: chain
    match:
      field: file_name
      pattern: '.*'
    actions:
      - rename:
          name: new_name.mkv
      - delete:
          mode: quarantine
"""
    )
    policy = BoundaryPolicy(quarantine_root=quarantine_root)
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules, policy=policy)))

    assert report.status == "completed"
    assert not (watch_root / "old_name.mkv").exists()
    assert not (watch_root / "new_name.mkv").exists()
    # Quarantine should use the original source path relative to watch root
    attempts = processor.attempts()
    resulting_paths = attempts[0]["resulting_paths"]
    assert len(resulting_paths) == 2
    quarantined = Path(resulting_paths[-1])
    assert quarantined.name == "old_name.mkv"


def test_quarantine_dry_run_reports_intended_action(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    quarantine_root = tmp_path / "quarantine"
    watch_root.mkdir()
    quarantine_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_quarantine_rules(watch_root / "rules.yaml")
    policy = BoundaryPolicy(quarantine_root=quarantine_root)
    processor = ItemProcessor(tmp_path / "attempts.db")

    plan = processor.plan(make_request(watch_root, item, rules, policy=policy))
    report = processor.execute(plan, ExecutionMode.DRY_RUN)

    assert report.dry_run is True
    assert report.actions[0].kind == "quarantine"
    assert report.actions[0].result == "DRY_RUN"
    assert item.exists()


def test_delete_refuses_when_source_fingerprint_changes(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_delete_direct_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    item.write_text("changed")

    report = processor.execute(plan)

    assert report.status == "needs-reconciliation"
    assert item.read_text() == "changed"


def test_delete_folder_requires_stable_tree(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    folder = watch_root / "subdir"
    folder.mkdir()
    (folder / "file.txt").write_text("content")
    rules = write_delete_direct_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, folder, rules))
    (folder / "new_file.txt").write_text("new_content")

    report = processor.execute(plan)

    assert report.status == "needs-reconciliation"
    assert folder.exists()


def test_delete_hard_link_requires_opt_in(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    link_path = watch_root / "link.mkv"
    link_path.hardlink_to(item)
    rules = watch_root / "rules.yaml"
    rules.write_text(
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
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    report = processor.execute(plan)

    assert report.status == "failed"
    assert "hard-link" in report.actions[-1].detail
    assert item.exists()


def test_quarantine_is_excluded_from_discovery(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    quarantine_root = tmp_path / "quarantine"
    watch_root.mkdir()
    quarantine_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_quarantine_rules(watch_root / "rules.yaml")
    policy = BoundaryPolicy(quarantine_root=quarantine_root)
    processor = ItemProcessor(tmp_path / "attempts.db")
    processor.execute(processor.plan(make_request(watch_root, item, rules, policy=policy)))

    quarantined_paths = list(quarantine_root.rglob("*"))
    assert len(quarantined_paths) > 0

    # process_batch should skip quarantine paths
    batch = processor.process_batch(
        "downloads", quarantine_root, rules, [ItemSnapshot(path=p, size=p.stat().st_size, mtime=p.stat().st_mtime) for p in quarantined_paths if p.is_file()],
        stability_interval=0.0, now=1000.0, boundary_policy=policy,
    )
    # All quarantine items should be failed or skipped, not executed
    for result in batch.items:
        assert result.status in ("failed", "skipped") or "outside" in result.status


def test_plans_first_matching_move_as_immutable_preview(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
  - name: fallback
    match:
      field: file_name
      pattern: '.*'
    actions:
      - move:
          destination: ../other
"""
    )

    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    assert plan.rule_name == "videos"
    assert plan.actions[0].kind == "move"
    assert plan.actions[0].target == tmp_path / "videos" / "movie.mkv"
    assert item.exists()

    report = processor.execute(plan, ExecutionMode.DRY_RUN)

    assert report.dry_run is True
    assert report.actions[0].result == "DRY_RUN"
    assert item.exists()


def test_invalid_rule_does_not_prevent_valid_rule_from_planning(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: invalid
    match:
      field: file_name
      pattern: '('
    actions:
      - move:
          destination: ../broken
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
"""
    )

    plan = ItemProcessor(attempts_path=tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))

    assert plan.rule_name == "videos"
    assert len(plan.diagnostics) == 1
    assert "invalid" in plan.diagnostics[0]


def test_apply_move_records_completed_attempt_after_success(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
"""
    )
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    report = processor.execute(plan)

    assert report.status == "completed"
    assert report.actions[0].result == "OK"
    assert not item.exists()
    assert (destination / "movie.mkv").read_text() == "movie"
    assert processor.attempts() == [{"status": "completed", "resulting_paths": [str(destination / "movie.mkv")], "processing_lineage": ["downloads"]}]


def test_web_preview_renders_the_shared_dry_run_plan(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
"""
    )
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    client = TestClient(create_app(processor))

    response = client.get(
        "/watches/downloads/dry-run",
        params={"watch_root": watch_root, "item": item, "rules_path": rules},
    )

    assert response.status_code == 200
    assert "videos" in response.text
    assert str(tmp_path / "videos" / "movie.mkv") in response.text
    assert item.exists()


def test_cli_check_accepts_the_documented_subcommand(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
"""
    )

    result = CliRunner().invoke(
        app,
        [
            "check",
            "downloads",
            str(watch_root),
            str(item),
            str(rules),
            "--attempts-path",
            str(tmp_path / "attempts.db"),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "videos" in result.stdout


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


def test_policy_requires_watch_and_destination_roots_inside_data_volumes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    config = tmp_path / "config"
    watch_root = data / "downloads"
    watch_root.mkdir(parents=True)
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")

    policy = BoundaryPolicy(data_roots=(data,), config_root=config, allowed_destinations=(data / "videos",))
    plan = ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules, policy=policy))

    assert plan.actions[0].target == data / "videos" / "movie.mkv"

    with pytest.raises(ValueError, match="config volume"):
        ItemProcessor(tmp_path / "config-attempts.db").plan(
            make_request(watch_root, item, write_move_rules(watch_root / "config-rules.yaml", str(config / "out")), policy=policy)
        )


def test_policy_rejects_overlapping_watch_roots_and_config_watch(tmp_path: Path) -> None:
    data = tmp_path / "data"
    config = tmp_path / "config"
    watch_root = data / "downloads"
    watch_root.mkdir(parents=True)
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")

    with pytest.raises(ValueError, match="watch roots must be disjoint"):
        ItemProcessor(tmp_path / "attempts.db").plan(
            make_request(
                watch_root,
                item,
                rules,
                policy=BoundaryPolicy(
                    data_roots=(data,),
                    config_root=config,
                    watch_roots=(watch_root, watch_root / "nested"),
                ),
            )
        )

    with pytest.raises(ValueError, match="config volume"):
        ItemProcessor(tmp_path / "config-attempts.db").plan(
            make_request(
                config,
                config,
                rules,
                policy=BoundaryPolicy(data_roots=(data,), config_root=config),
            )
        )


def test_plan_rejects_self_and_descendant_targets(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "folder"
    item.mkdir()
    rules = write_move_rules(watch_root / "rules.yaml", "folder/subfolder")

    with pytest.raises(ValueError, match="self-targeting"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def test_plan_rejects_symlink_traversal(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    outside = tmp_path / "outside"
    watch_root.mkdir()
    outside.mkdir()
    (watch_root / "linked").symlink_to(outside, target_is_directory=True)
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "linked")

    with pytest.raises(ValueError, match="symlink"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def test_plan_rejects_existing_and_case_only_collisions(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "Movie.mkv"
    item.write_text("movie")
    (destination / "movie.mkv").write_text("existing")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")

    with pytest.raises(ValueError, match="collision"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def test_cross_watch_destination_is_allowed_with_warning(tmp_path: Path) -> None:
    data = tmp_path / "data"
    watch_root = data / "downloads"
    destination = data / "videos"
    watch_root.mkdir(parents=True)
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")

    plan = ItemProcessor(tmp_path / "attempts.db").plan(
        make_request(
            watch_root,
            item,
            rules,
            policy=BoundaryPolicy(
                data_roots=(data,),
                watch_roots=(watch_root, destination),
                allowed_destinations=(destination,),
            ),
        )
    )

    assert any("another watch folder" in diagnostic for diagnostic in plan.diagnostics)


def test_named_condition_expands_numbered_and_named_captures_in_rename(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "Alice [cosplay].mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: normalize
    match:
      name: title
      field: file_name
      pattern: '^(?P<title>.*) \\[cosplay\\](?P<extension>\\.mkv)$'
    actions:
      - rename:
          name: '\\g<title>\\g<extension>'
      - rename:
          name: '\\1\\2'
"""
    )

    plan = ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))

    assert [action.target for action in plan.actions] == [
        watch_root / "Alice.mkv",
        watch_root / "Alice.mkv",
    ]


def test_invalid_capture_reference_disables_rule_and_warns(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: invalid
    match:
      name: title
      field: file_name
      pattern: '(.*)'
    actions:
      - rename:
          name: '\\g<missing>'
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
"""
    )

    plan = ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))

    assert plan.rule_name == "videos"
    assert any("disabled earlier rule 1" in diagnostic for diagnostic in plan.diagnostics)
    assert "capture" in plan.diagnostics[0]


def test_full_path_matches_normalized_container_absolute_path(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    watch_root = data_root / "downloads"
    watch_root.mkdir(parents=True)
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: videos
    match:
      field: full_path
      pattern: '^/.*?/data/downloads/movie\\.mkv$'
    actions:
      - move:
          destination: ../videos
"""
    )

    plan = ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))

    assert plan.source == item.resolve()


def test_plan_revision_blocks_execution_after_rules_change(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")

    plan = processor.plan(make_request(watch_root, item, rules))
    rules.write_text(rules.read_text().replace("../videos", "../other"))

    with pytest.raises(ValueError, match="stale plan: ruleset revision changed"):
        processor.execute(plan)


def test_execute_rejects_stale_source_before_creating_attempt(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    item.write_text("changed")

    with pytest.raises(ValueError, match="stale plan: source changed"):
        processor.execute(plan)

    assert processor.attempts() == []


def test_rename_executes_named_capture_and_records_result_identity(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "Alice [cosplay].mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: normalize
    match:
      name: title
      field: file_name
      pattern: '^(?P<title>.*) \\[cosplay\\](?P<extension>\\.mkv)$'
    actions:
      - rename:
          name: '\\g<title>\\g<extension>'
"""
    )
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    renamed = watch_root / "Alice.mkv"
    assert report.status == "completed"
    assert renamed.exists()
    assert processor.attempts() == [{"status": "completed", "resulting_paths": [str(renamed)], "processing_lineage": ["downloads"]}]


def test_copy_stages_without_overwrite_and_preserves_provenance(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    copied = destination / item.name
    assert report.status == "completed"
    assert item.exists() and copied.read_text() == "movie"
    assert report.actions[0].resulting_path == copied
    assert report.actions[0].source == item
    assert processor.attempts()[0]["copy_provenance"] == {"source": str(item), "result": str(copied)}


def test_copy_refuses_when_source_changes_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    original_copy = processor._copy_to_staging

    def mutate_source(source: Path, target: Path) -> Path:
        result = original_copy(source, target)
        item.write_text("changed")
        return result

    monkeypatch.setattr(processor, "_copy_to_staging", mutate_source)

    report = processor.execute(plan)

    assert report.status == "failed"
    assert not (destination / item.name).exists()
    assert item.read_text() == "changed"


def test_action_chain_uses_primary_result_and_stops_after_failure(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: chain
    match:
      field: file_name
      pattern: '.*'
    actions:
      - copy:
          destination: ../videos
      - rename:
          name: renamed.mkv
      - delete:
          mode: direct
    allow_direct_deletion: true
"""
    )
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    assert report.status == "completed"
    assert not item.exists()
    assert (destination / "movie.mkv").exists()
    assert not (destination / "renamed.mkv").exists()
    assert [result.kind for result in report.actions] == ["copy", "rename", "delete"]


def test_invalid_action_chain_is_rejected_at_planning(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: invalid
    match:
      field: file_name
      pattern: '.*'
    actions:
      - delete:
          mode: direct
      - rename:
          name: renamed.mkv
    allow_direct_deletion: true
"""
    )

    with pytest.raises(ValueError, match="cannot accept"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def write_copy_rules(path: Path, destination: str) -> Path:
    path.write_text(
        f"""rules:
  - name: copy
    match:
      field: file_name
      pattern: '.*'
    actions:
      - copy:
          destination: {destination}
"""
    )
    return path


def write_archive_rules(path: Path, destination: str, extension: str = ".zip", preserve_original: bool = True) -> Path:
    path.write_text(
        f"""rules:
  - name: archive
    match:
      field: file_name
      pattern: '.*'
    actions:
      - archive:
          destination: {destination}
          extension: {extension}
          preserve_originals: {str(preserve_original).lower()}
"""
    )
    return path


def test_archive_creates_named_file_and_records_result(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "archives"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_archive_rules(watch_root / "rules.yaml", "../archives")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    archive = destination / "movie.mkv.zip"
    assert report.status == "completed"
    assert item.exists()
    assert archive.exists()
    assert processor.attempts()[0]["resulting_paths"] == [str(archive)]


def test_archive_folders_and_removes_original_when_not_preserved(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "archives"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "season"
    item.mkdir()
    (item / "episode.txt").write_text("episode")
    rules = write_archive_rules(watch_root / "rules.yaml", "../archives", preserve_original=False)
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    archive = destination / "season.zip"
    assert report.status == "completed"
    assert not item.exists()
    assert archive.exists()


def test_archive_rejects_destination_collision(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "archives"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    (destination / "movie.mkv.zip").write_text("existing")
    rules = write_archive_rules(watch_root / "rules.yaml", "../archives")

    with pytest.raises(ValueError, match="collision"):
        ItemProcessor(tmp_path / "attempts.db").plan(make_request(watch_root, item, rules))


def test_archive_creates_7z_and_preserves_original(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "archives"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_archive_rules(watch_root / "rules.yaml", "../archives", extension=".7z")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    archive = destination / "movie.mkv.7z"
    assert report.status == "completed"
    assert item.exists()
    with py7zr.SevenZipFile(archive, "r") as seven_zip:
        assert seven_zip.readall()["movie.mkv"].read() == b"movie"


def test_unarchive_7z_uses_staging_and_retains_source(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    archive = watch_root / "bundle.7z"
    source = tmp_path / "source.txt"
    source.write_text("content")
    with py7zr.SevenZipFile(archive, "w") as seven_zip:
        seven_zip.write(source, arcname="folder/file.txt")
    rules = write_unarchive_rules(watch_root / "rules.yaml")
    rules.write_text(rules.read_text().replace("\\.zip$", "\\.(zip|7z)$"))
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, archive, rules)))

    assert report.status == "completed"
    assert (watch_root / "bundle" / "folder" / "file.txt").read_text() == "content"
    assert archive.exists()


def test_unarchive_rar_tooling_failure_is_visible_and_retains_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    archive = watch_root / "bundle.rar"
    archive.write_bytes(b"rar")
    rules = write_unarchive_rules(watch_root / "rules.yaml")
    rules.write_text(rules.read_text().replace("\\.zip$", "\\.(zip|rar)$"))
    processor = ItemProcessor(tmp_path / "attempts.db")

    def unavailable(_: Path) -> object:
        raise rarfile.RarCannotExec("unrar unavailable")

    monkeypatch.setattr(processor, "_open_archive", unavailable)
    report = processor.execute(processor.plan(make_request(watch_root, archive, rules)))

    assert report.status == "failed"
    assert "RarCannotExec" in report.actions[-1].detail
    assert archive.exists()


def test_archive_refuses_publication_and_removal_when_source_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "archives"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_archive_rules(watch_root / "rules.yaml", "../archives", preserve_original=False)
    processor = ItemProcessor(tmp_path / "attempts.db")
    original = processor._archive_to_staging

    def mutate_source(source: Path, target: Path) -> Path:
        staging = original(source, target)
        item.write_text("changed")
        return staging

    monkeypatch.setattr(processor, "_archive_to_staging", mutate_source)
    report = processor.execute(processor.plan(make_request(watch_root, item, rules)))

    assert report.status == "failed"
    assert not (destination / "movie.zip").exists()
    assert item.read_text() == "changed"


def write_unarchive_rules(path: Path, destination: str = ".", **settings: object) -> Path:
    values = "\n".join(f"          {key}: {value}" for key, value in settings.items())
    path.write_text(f"""rules:
  - name: unarchive
    match:
      field: file_name
      pattern: '\\.zip$'
    actions:
      - unarchive:
          destination: {destination}
{values}
""")
    return path


def make_zip(path: Path, entries: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def test_unarchive_preview_is_bounded_and_read_only(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    archive = watch_root / "bundle.zip"
    make_zip(archive, {"folder/file.txt": "content"})
    rules = write_unarchive_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, archive, rules))

    preview = processor.preview(plan)

    assert preview is not None
    assert preview.extraction_root == watch_root / "bundle"
    assert preview.entry_count == 1
    assert preview.truncated is False
    assert sorted(watch_root.iterdir()) == sorted([archive, rules])
    assert processor.attempts() == []


def test_unarchive_stages_and_publishes_under_extraction_root(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    archive = watch_root / "bundle.zip"
    make_zip(archive, {"folder/file.txt": "content"})
    rules = write_unarchive_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, archive, rules)))

    assert report.status == "completed"
    assert (watch_root / "bundle" / "folder" / "file.txt").read_text() == "content"
    assert archive.exists()


def test_unarchive_rejects_traversal_and_limits_without_publishing(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    archive = watch_root / "bundle.zip"
    make_zip(archive, {"../outside.txt": "nope"})
    rules = write_unarchive_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, archive, rules)))

    assert report.status == "failed"
    assert "traversal" in report.actions[-1].detail
    assert not (tmp_path / "outside.txt").exists()
    assert not (watch_root / "bundle").exists()


def test_unarchive_classifies_corrupt_archive_and_suppresses_retry(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    archive = watch_root / "bundle.zip"
    archive.write_bytes(b"not a zip")
    rules = write_unarchive_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, archive, rules)))

    assert report.status == "failed"
    assert "BadZipFile" in report.actions[-1].detail
    assert archive.exists()
    assert processor.has_suppressed_attempt("downloads", archive, processor._fingerprint(archive)) is True


def test_unarchive_rejects_zip_symlink_entry(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    archive = watch_root / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        entry = zipfile.ZipInfo("link")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        zip_file.writestr(entry, "../../outside")
    rules = write_unarchive_rules(watch_root / "rules.yaml")
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, archive, rules)))

    assert report.status == "failed"
    assert "symlink" in report.actions[-1].detail


def test_ui_rule_save_uses_compare_and_swap_revision(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")
    client = TestClient(create_app(processor))
    rules = tmp_path / "rules.yaml"
    rules.write_text("rules: []\n")
    revision = hashlib.sha256(rules.read_bytes()).hexdigest()

    response = client.put(
        "/watches/downloads/rules",
        params={"rules_path": rules, "expected_revision": revision},
        content="rules:\n  - name: videos\n",
    )

    assert response.status_code == 200
    assert response.json()["revision"] != revision

    conflict = client.put(
        "/watches/downloads/rules",
        params={"rules_path": rules, "expected_revision": revision},
        content="rules: []\n",
    )

    assert conflict.status_code == 409


def test_acquire_lease_succeeds_for_new_source_identity(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    source = tmp_path / "movie.mkv"
    source.write_text("movie")

    acquired = processor.acquire_lease("downloads", source, "fp1")

    assert acquired is True


def test_acquire_lease_fails_for_already_leased_identity(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    source = tmp_path / "movie.mkv"
    source.write_text("movie")
    processor.acquire_lease("downloads", source, "fp1")

    assert processor.acquire_lease("downloads", source, "fp1") is False


def test_acquire_lease_succeeds_for_different_fingerprint(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    source = tmp_path / "movie.mkv"
    source.write_text("movie")
    processor.acquire_lease("downloads", source, "fp1")

    assert processor.acquire_lease("downloads", source, "fp2") is True


def test_acquire_lease_succeeds_for_different_path(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    source_a = tmp_path / "a.mkv"
    source_a.write_text("a")
    source_b = tmp_path / "b.mkv"
    source_b.write_text("b")
    processor.acquire_lease("downloads", source_a, "fp1")

    assert processor.acquire_lease("downloads", source_b, "fp1") is True


def test_execute_acquires_and_releases_lease(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    report = processor.execute(plan)

    assert report.status == "completed"
    assert processor.acquire_lease("downloads", item, plan.source_fingerprint) is True


def test_execute_fails_when_lease_unavailable(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.acquire_lease("downloads", plan.source, plan.source_fingerprint)

    with pytest.raises(ValueError, match="lease"):
        processor.execute(plan)


def test_has_completed_attempt_is_false_when_no_attempt(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    source = tmp_path / "movie.mkv"
    source.write_text("movie")

    assert processor.has_completed_attempt("downloads", source, "fp1") is False


def test_has_completed_attempt_is_true_after_completed(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.execute(plan)

    assert processor.has_completed_attempt("downloads", item, plan.source_fingerprint) is True


def test_has_completed_attempt_is_false_after_failed(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")
    source = tmp_path / "movie.mkv"
    source.write_text("movie")
    fingerprint = "test-fp"
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("failed-1", "downloads", str(source), "move", "failed", "[]", fingerprint),
        )

    assert processor.has_completed_attempt("downloads", source, fingerprint) is False


def test_has_completed_attempt_is_false_for_different_fingerprint(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.execute(plan)

    assert processor.has_completed_attempt("downloads", item, "different-fp") is False


def test_is_stable_returns_true_with_zero_interval(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    snapshot = ItemSnapshot(path=tmp_path / "movie.mkv", size=5, mtime=1000.0)

    assert processor.is_stable("downloads", snapshot, now=1000.0, stability_interval=0.0) is True


def test_is_stable_returns_false_for_first_observation_with_interval(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    snapshot = ItemSnapshot(path=tmp_path / "movie.mkv", size=5, mtime=1000.0)

    assert processor.is_stable("downloads", snapshot, now=1000.0, stability_interval=5.0) is False


def test_is_stable_returns_true_after_interval_elapsed(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    snapshot = ItemSnapshot(path=tmp_path / "movie.mkv", size=5, mtime=1000.0)
    processor.is_stable("downloads", snapshot, now=1000.0, stability_interval=5.0)

    assert processor.is_stable("downloads", snapshot, now=1005.0, stability_interval=5.0) is True


def test_is_stable_resets_on_changed_observation(tmp_path: Path) -> None:
    processor = ItemProcessor(attempts_path=tmp_path / "attempts.db")
    snapshot1 = ItemSnapshot(path=tmp_path / "movie.mkv", size=5, mtime=1000.0)
    processor.is_stable("downloads", snapshot1, now=1000.0, stability_interval=5.0)

    snapshot2 = ItemSnapshot(path=tmp_path / "movie.mkv", size=10, mtime=1003.0)
    assert processor.is_stable("downloads", snapshot2, now=1003.0, stability_interval=5.0) is False
    assert processor.is_stable("downloads", snapshot2, now=1008.0, stability_interval=5.0) is True


def test_process_batch_executes_eligible_item(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    snapshots = [ItemSnapshot(path=item, size=5, mtime=item.stat().st_mtime)]

    batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=0.0, now=1000.0,
    )

    assert len(batch.items) == 1
    assert batch.items[0].status == "executed"
    assert batch.items[0].report is not None
    assert batch.items[0].report.status == "completed"
    assert (destination / "movie.mkv").read_text() == "movie"


def test_process_batch_skips_completed_unchanged_item(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    mtime = item.stat().st_mtime
    snapshots = [ItemSnapshot(path=item, size=5, mtime=mtime)]

    first_batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=0.0, now=1000.0,
    )
    assert first_batch.items[0].status == "executed"

    (destination / "movie.mkv").unlink()
    item.write_text("movie")
    snapshots2 = [ItemSnapshot(path=item, size=5, mtime=mtime)]
    second_batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots2, stability_interval=0.0, now=2000.0,
    )

    assert len(second_batch.items) == 1
    assert second_batch.items[0].status == "skipped"
    assert second_batch.items[0].report is None


def test_process_batch_defers_unstable_item(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    snapshots = [ItemSnapshot(path=item, size=5, mtime=item.stat().st_mtime)]

    batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=5.0, now=1000.0,
    )

    assert len(batch.items) == 1
    assert batch.items[0].status == "deferred"
    assert batch.items[0].report is None
    assert item.exists()


def test_process_batch_processes_item_after_stability_interval(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    mtime = item.stat().st_mtime
    snapshots = [ItemSnapshot(path=item, size=5, mtime=mtime)]

    processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=5.0, now=1000.0,
    )

    snapshots2 = [ItemSnapshot(path=item, size=5, mtime=mtime)]
    batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots2, stability_interval=5.0, now=1005.0,
    )

    assert batch.items[0].status == "executed"
    assert (destination / "movie.mkv").read_text() == "movie"


def test_process_batch_marks_leased_item_as_outside_snapshot(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.acquire_lease("downloads", plan.source, plan.source_fingerprint)
    snapshots = [ItemSnapshot(path=item, size=5, mtime=item.stat().st_mtime)]

    batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=0.0, now=1000.0,
    )

    assert batch.items[0].status == "outside_snapshot"


def test_process_batch_reports_mixed_outcomes(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item_a = watch_root / "a.mkv"
    item_a.write_text("aaa")
    item_b = watch_root / "b.mkv"
    item_b.write_text("bbb")
    item_c = watch_root / "c.mkv"
    item_c.write_text("ccc")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")

    plan_a = processor.plan(make_request(watch_root, item_a, rules))
    processor.acquire_lease("downloads", plan_a.source, plan_a.source_fingerprint)

    snapshots = [
        ItemSnapshot(path=item_a, size=3, mtime=item_a.stat().st_mtime),
        ItemSnapshot(path=item_b, size=3, mtime=item_b.stat().st_mtime),
        ItemSnapshot(path=item_c, size=3, mtime=item_c.stat().st_mtime),
    ]

    batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=0.0, now=1000.0,
    )

    statuses = {item.source.name: item.status for item in batch.items}
    assert statuses["a.mkv"] == "outside_snapshot"
    assert statuses["b.mkv"] == "executed"
    assert statuses["c.mkv"] == "executed"


def test_process_batch_deduplicates_deferred_diagnostics(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item_a = watch_root / "a.mkv"
    item_a.write_text("aaa")
    item_b = watch_root / "b.mkv"
    item_b.write_text("bbb")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    snapshots = [
        ItemSnapshot(path=item_a, size=3, mtime=item_a.stat().st_mtime),
        ItemSnapshot(path=item_b, size=3, mtime=item_b.stat().st_mtime),
    ]

    batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=5.0, now=1000.0,
    )

    assert len(batch.items) == 2
    assert all(item.status == "deferred" for item in batch.items)
    deferred_diags = [d for d in batch.diagnostics if "deferred" in d.lower() or "unstable" in d.lower()]
    assert len(deferred_diags) <= 1


def test_recover_stale_leases_moves_started_attempt_to_reconciliation(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("stale-attempt", "downloads", str(plan.source), "move", "started", "[]", plan.source_fingerprint),
        )
        conn.execute(
            "INSERT INTO processing_leases (watch_id, source_path, source_fingerprint, attempt_id, acquired_at) VALUES (?, ?, ?, ?, ?)",
            ("downloads", str(plan.source), plan.source_fingerprint, "stale-attempt", "2024-01-01T00:00:00"),
        )

    recovered = processor.recover_stale_leases()

    assert "stale-attempt" in recovered
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute("SELECT status FROM processing_attempts WHERE attempt_id = ?", ("stale-attempt",)).fetchone()
    assert row[0] == "needs-reconciliation"


def test_recover_stale_leases_ignores_completed_attempts(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.execute(plan)

    recovered = processor.recover_stale_leases()

    assert recovered == []


def test_recover_stale_leases_releases_lease_for_recovered_attempt(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("stale-attempt", "downloads", str(plan.source), "move", "started", "[]", plan.source_fingerprint),
        )
        conn.execute(
            "INSERT INTO processing_leases (watch_id, source_path, source_fingerprint, attempt_id, acquired_at) VALUES (?, ?, ?, ?, ?)",
            ("downloads", str(plan.source), plan.source_fingerprint, "stale-attempt", "2024-01-01T00:00:00"),
        )

    recovered = processor.recover_stale_leases()

    assert "stale-attempt" in recovered
    assert processor.acquire_lease("downloads", plan.source, plan.source_fingerprint) is True


def test_recover_stale_leases_finds_attempts_from_real_execute_flow(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    processor.acquire_lease("downloads", plan.source, plan.source_fingerprint)
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("crash-attempt", "downloads", str(plan.source), "move", "started", "[]", plan.source_fingerprint),
        )

    recovered = processor.recover_stale_leases()

    assert len(recovered) == 1
    assert recovered[0] == "crash-attempt"
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute("SELECT status FROM processing_attempts WHERE attempt_id = ?", ("crash-attempt",)).fetchone()
    assert row[0] == "needs-reconciliation"
    assert processor.acquire_lease("downloads", plan.source, plan.source_fingerprint) is True


def test_process_batch_reports_failed_status_for_planning_error(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "nomatch.txt"
    item.write_text("no matching rule")
    rules = watch_root / "rules.yaml"
    rules.write_text(
        """rules:
  - name: videos
    match:
      field: file_name
      pattern: '\\.mkv$'
    actions:
      - move:
          destination: ../videos
"""
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    snapshots = [ItemSnapshot(path=item, size=16, mtime=item.stat().st_mtime)]

    batch = processor.process_batch(
        "downloads", watch_root, rules, snapshots, stability_interval=0.0, now=1000.0,
    )

    assert batch.items[0].status == "failed"
    assert "no valid rule" in batch.items[0].detail


def test_collision_suppresses_automatic_retry(tmp_path: Path) -> None:
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
    batch = processor.process_batch(
        "downloads", watch_root, rules, [snapshot], stability_interval=0.0, now=1000.0,
    )

    assert batch.items[0].status == "failed"
    assert "collision" in batch.items[0].detail.lower()
    fingerprint = processor._fingerprint(item)
    assert processor.has_suppressed_attempt("downloads", item, fingerprint) is True

    batch2 = processor.process_batch(
        "downloads", watch_root, rules, [snapshot], stability_interval=0.0, now=2000.0,
    )
    assert batch2.items[0].status == "failed"
    assert "suppressed" in batch2.items[0].detail.lower()


def test_has_suppressed_attempt_false_when_no_history(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")
    source = tmp_path / "movie.mkv"
    source.write_text("movie")

    assert processor.has_suppressed_attempt("downloads", source, "fp1") is False


def test_has_suppressed_attempt_false_for_completed(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.execute(plan)

    assert processor.has_suppressed_attempt("downloads", item, plan.source_fingerprint) is False


def test_execute_collision_creates_suppression(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))

    (destination / "movie.mkv").write_text("existing")

    report = processor.execute(plan)

    assert report.status == "failed"
    assert "exists" in report.actions[-1].detail
    assert processor.has_suppressed_attempt("downloads", item, plan.source_fingerprint) is True


def test_retry_attempt_creates_linked_fresh_plan(tmp_path: Path) -> None:
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
    import sqlite3
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute("SELECT attempt_id FROM processing_attempts WHERE status = ?", ("failed",)).fetchone()
    original_attempt_id = row[0]

    (destination / "movie.mkv").unlink()
    report = processor.retry_attempt(original_attempt_id, watch_root, rules)

    assert report.status == "completed"
    assert (destination / "movie.mkv").read_text() == "movie"
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        latest = conn.execute("SELECT attempt_id, status, retry_of_attempt_id FROM processing_attempts ORDER BY rowid DESC LIMIT 1").fetchone()
    assert latest[1] == "completed"
    assert latest[2] == original_attempt_id


def test_retry_attempt_clears_suppression(tmp_path: Path) -> None:
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
    import sqlite3
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute("SELECT attempt_id FROM processing_attempts WHERE status = ?", ("failed",)).fetchone()
    original_attempt_id = row[0]
    fingerprint = processor._fingerprint(item)
    assert processor.has_suppressed_attempt("downloads", item, fingerprint) is True

    (destination / "movie.mkv").unlink()
    processor.retry_attempt(original_attempt_id, watch_root, rules)

    assert processor.has_suppressed_attempt("downloads", item, fingerprint) is False


def test_retry_attempt_rejects_nonexistent_attempt(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")

    with pytest.raises(ValueError, match="attempt not found"):
        processor.retry_attempt("nonexistent", tmp_path, tmp_path / "rules.yaml")


def test_retry_attempt_rejects_completed_attempt(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_move_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.execute(plan)
    import sqlite3
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute("SELECT attempt_id FROM processing_attempts WHERE status = ?", ("completed",)).fetchone()

    with pytest.raises(ValueError, match="not retryable"):
        processor.retry_attempt(row[0], watch_root, rules)


def test_retry_attempt_reports_missing_source(tmp_path: Path) -> None:
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
    import sqlite3
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        row = conn.execute("SELECT attempt_id FROM processing_attempts WHERE status = ?", ("failed",)).fetchone()
    original_attempt_id = row[0]
    item.unlink()
    (destination / "movie.mkv").unlink()

    with pytest.raises((ValueError, OSError)):
        processor.retry_attempt(original_attempt_id, watch_root, rules)


def test_reprocess_item_creates_fresh_plan(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_root / "rules.yaml", "../videos")
    processor = ItemProcessor(tmp_path / "attempts.db")
    plan = processor.plan(make_request(watch_root, item, rules))
    processor.execute(plan)
    first_fingerprint = plan.source_fingerprint
    assert processor.has_completed_attempt("downloads", item, first_fingerprint) is True
    assert (destination / "movie.mkv").read_text() == "movie"

    (destination / "movie.mkv").unlink()
    report = processor.reprocess_item("downloads", watch_root, item, rules)

    assert report.status == "completed"
    assert (destination / "movie.mkv").read_text() == "movie"


def test_suppressed_attempts_lists_suppressed_identities(tmp_path: Path) -> None:
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

    suppressions = processor.suppressed_attempts()

    assert len(suppressions) == 1
    assert suppressions[0]["watch_id"] == "downloads"
    assert suppressions[0]["reason"] == "collision"
    assert suppressions[0]["source_path"] == str(item.resolve())


def test_nested_extraction_extracts_inner_archive_within_parent_extraction_root(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    inner_archive = tmp_path / "inner.txt"
    inner_archive.write_text("inner content")
    inner_zip = tmp_path / "inner.zip"
    make_zip(inner_zip, {"inner.txt": "inner content"})
    outer_zip = watch_root / "outer.zip"
    with zipfile.ZipFile(outer_zip, "w") as archive:
        archive.write(inner_zip, "inner.zip")
    rules = write_unarchive_rules(watch_root / "rules.yaml", max_depth=1)
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, outer_zip, rules)))

    assert report.status == "completed"
    assert (watch_root / "outer" / "inner" / "inner.txt").read_text() == "inner content"


def test_nested_extraction_respects_configured_depth_limit(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    deep_zip = tmp_path / "deep.zip"
    make_zip(deep_zip, {"deep.txt": "deep"})
    mid_zip = tmp_path / "mid.zip"
    with zipfile.ZipFile(mid_zip, "w") as archive:
        archive.write(deep_zip, "deep.zip")
    outer_zip = watch_root / "outer.zip"
    with zipfile.ZipFile(outer_zip, "w") as archive:
        archive.write(mid_zip, "mid.zip")
    rules = write_unarchive_rules(watch_root / "rules.yaml", max_depth=1)
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, outer_zip, rules)))

    assert report.status == "completed"
    assert (watch_root / "outer" / "mid").is_dir()
    assert (watch_root / "outer" / "mid" / "mid.zip").exists()
    assert not (watch_root / "outer" / "mid" / "deep").is_dir()


def test_nested_extraction_with_zero_depth_does_not_extract_inner_archives(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    inner_zip = tmp_path / "inner.zip"
    make_zip(inner_zip, {"file.txt": "content"})
    outer_zip = watch_root / "outer.zip"
    with zipfile.ZipFile(outer_zip, "w") as archive:
        archive.write(inner_zip, "inner.zip")
    rules = write_unarchive_rules(watch_root / "rules.yaml", max_depth=0)
    processor = ItemProcessor(tmp_path / "attempts.db")

    report = processor.execute(processor.plan(make_request(watch_root, outer_zip, rules)))

    assert report.status == "completed"
    assert (watch_root / "outer" / "inner.zip").exists()
    assert not (watch_root / "outer" / "inner").is_dir()


def test_cross_watch_handoff_records_processing_lineage_in_attempt(tmp_path: Path) -> None:
    data = tmp_path / "data"
    watch_a = data / "downloads"
    watch_b = data / "videos"
    watch_a.mkdir(parents=True)
    watch_b.mkdir()
    item = watch_a / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_a / "rules.yaml", "../videos")
    policy = BoundaryPolicy(
        data_roots=(data,),
        watch_roots=(watch_a, watch_b),
        watch_ids=("downloads", "videos"),
        allowed_destinations=(watch_b,),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    request = PlanRequest(
        watch_id="downloads",
        watch_root=watch_a,
        item=item,
        rules_path=rules,
        boundary_policy=policy,
        processing_lineage=(),
    )

    report = processor.execute(processor.plan(request))

    assert report.status == "completed"
    attempts = processor.attempts()
    assert attempts[0]["processing_lineage"] == ["downloads", "videos"]


def test_cross_watch_handoff_rejects_return_to_visited_watch(tmp_path: Path) -> None:
    data = tmp_path / "data"
    watch_a = data / "downloads"
    watch_b = data / "videos"
    watch_a.mkdir(parents=True)
    watch_b.mkdir()
    item = watch_b / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_b / "rules.yaml", "../downloads")
    policy = BoundaryPolicy(
        data_roots=(data,),
        watch_roots=(watch_a, watch_b),
        watch_ids=("downloads", "videos"),
        allowed_destinations=(watch_a,),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    request = PlanRequest(
        watch_id="videos",
        watch_root=watch_b,
        item=item,
        rules_path=rules,
        boundary_policy=policy,
        processing_lineage=("downloads",),
    )

    with pytest.raises(ValueError, match="lineage"):
        processor.plan(request)


def test_cross_watch_handoff_allows_forward_pipeline_to_new_watch(tmp_path: Path) -> None:
    data = tmp_path / "data"
    watch_a = data / "downloads"
    watch_b = data / "videos"
    watch_c = data / "archive"
    watch_a.mkdir(parents=True)
    watch_b.mkdir()
    watch_c.mkdir()
    item = watch_b / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_b / "rules.yaml", "../archive")
    policy = BoundaryPolicy(
        data_roots=(data,),
        watch_roots=(watch_a, watch_b, watch_c),
        watch_ids=("downloads", "videos", "archive"),
        allowed_destinations=(watch_c,),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    request = PlanRequest(
        watch_id="videos",
        watch_root=watch_b,
        item=item,
        rules_path=rules,
        boundary_policy=policy,
        processing_lineage=("downloads", "videos"),
    )

    plan = processor.plan(request)
    report = processor.execute(plan)

    assert report.status == "completed"
    attempts = processor.attempts()
    assert attempts[0]["processing_lineage"] == ["downloads", "videos", "archive"]


def test_processing_lineage_includes_current_watch_automatically(tmp_path: Path) -> None:
    data = tmp_path / "data"
    watch_a = data / "downloads"
    watch_b = data / "videos"
    watch_a.mkdir(parents=True)
    watch_b.mkdir()
    item = watch_a / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_a / "rules.yaml", "../videos")
    policy = BoundaryPolicy(
        data_roots=(data,),
        watch_roots=(watch_a, watch_b),
        watch_ids=("downloads", "videos"),
        allowed_destinations=(watch_b,),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    request = PlanRequest(
        watch_id="downloads",
        watch_root=watch_a,
        item=item,
        rules_path=rules,
        boundary_policy=policy,
    )

    plan = processor.plan(request)

    assert "downloads" in plan.processing_lineage


def test_cross_watch_handoff_records_resulting_path_handoff(tmp_path: Path) -> None:
    data = tmp_path / "data"
    watch_a = data / "downloads"
    watch_b = data / "videos"
    watch_a.mkdir(parents=True)
    watch_b.mkdir()
    item = watch_a / "movie.mkv"
    item.write_text("movie")
    rules = write_copy_rules(watch_a / "rules.yaml", "../videos")
    policy = BoundaryPolicy(
        data_roots=(data,),
        watch_roots=(watch_a, watch_b),
        watch_ids=("downloads", "videos"),
        allowed_destinations=(watch_b,),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    request = PlanRequest(
        watch_id="downloads",
        watch_root=watch_a,
        item=item,
        rules_path=rules,
        boundary_policy=policy,
    )

    report = processor.execute(processor.plan(request))

    assert report.status == "completed"
    handoffs = report.handoffs
    assert len(handoffs) == 1
    assert handoffs[0].watch_id == "videos"
    assert handoffs[0].resulting_path == watch_b / "movie.mkv"
