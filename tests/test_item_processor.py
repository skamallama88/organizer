from pathlib import Path
import hashlib

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from organizer.cli import app
from organizer.web import create_app
import pytest

from organizer.item_processor import (
    BoundaryPolicy,
    ExecutionMode,
    ItemProcessor,
    PlanRequest,
)


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
    assert processor.attempts() == [{"status": "completed", "resulting_paths": [str(destination / "movie.mkv")] }]


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
