from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from organizer.cli import app
from organizer.web import create_app
from organizer.item_processor import ExecutionMode, ItemProcessor, PlanRequest


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
    plan = processor.plan(PlanRequest(watch_id="downloads", watch_root=watch_root, item=item, rules_path=rules))

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

    plan = ItemProcessor(attempts_path=tmp_path / "attempts.db").plan(
        PlanRequest(watch_id="downloads", watch_root=watch_root, item=item, rules_path=rules)
    )

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
    plan = processor.plan(PlanRequest(watch_id="downloads", watch_root=watch_root, item=item, rules_path=rules))

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
