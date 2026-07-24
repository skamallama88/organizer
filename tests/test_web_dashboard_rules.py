from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from organizer.config import WatchFolderConfig
from organizer.item_processor import BoundaryPolicy, ItemProcessor
from organizer.operational_health import OperationalHealth
from organizer.structured_log import LogEntry, LogLevel, LogResult, MemoryLogSink, StructuredLogger
from organizer.web import create_app


def make_client(tmp_path: Path) -> tuple[TestClient, Path, MemoryLogSink]:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules: []\n")
    log_sink = MemoryLogSink()
    processor = ItemProcessor(tmp_path / "attempts.db", logger=StructuredLogger(sinks=[log_sink]))
    config = WatchFolderConfig(
        watch_id="downloads",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(data_roots=(tmp_path,), allowed_destinations=(tmp_path,), watch_roots=(watch_root,)),
    )
    return TestClient(create_app(
        processor,
        log_sink=log_sink,
        health_checker=OperationalHealth(),
        watch_folders=[config],
        db_path=tmp_path / "attempts.db",
    )), rules_path, log_sink


def test_dashboard_renders_watch_health_rule_count_and_recent_activity(tmp_path: Path) -> None:
    client, rules_path, log_sink = make_client(tmp_path)
    rules_path.write_text("rules:\n  - name: Archive\n    match: {field: file_name, pattern: .+}\n    actions: [{archive: {format: zip, destination: /tmp}}]\n")
    log_sink.write(LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="Archive",
        action="archive",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    ))

    response = client.get("/")

    assert response.status_code == 200
    assert "downloads" in response.text
    assert "Healthy" in response.text
    assert "1 rule" in response.text
    assert "Archive" in response.text
    assert 'href="/watches/downloads/rules"' in response.text
    assert 'href="/logs?watch=downloads"' in response.text


def test_rule_editor_renders_current_document_and_revision(tmp_path: Path) -> None:
    client, rules_path, _ = make_client(tmp_path)
    rules_path.write_text("rules: []\n")

    response = client.get("/watches/downloads/rules")

    assert response.status_code == 200
    assert "Rule editor: downloads" in response.text
    assert "rules: []" in response.text
    assert hashlib.sha256(rules_path.read_bytes()).hexdigest() in response.text
    assert 'hx-post="/watches/downloads/rules/validate"' in response.text


def test_rule_editor_validation_returns_inline_htmx_feedback(tmp_path: Path) -> None:
    client, rules_path, _ = make_client(tmp_path)

    response = client.post(
        "/watches/downloads/rules/validate",
        data={"rules": "rules:\n  - name: broken\n    match: {field: no, pattern: '['}\n    actions: []\n"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "invalid" in response.text
    assert "rule 1" in response.text


def test_rule_editor_dry_run_returns_inline_preview(tmp_path: Path) -> None:
    client, rules_path, _ = make_client(tmp_path)
    item = tmp_path / "downloads" / "movie.txt"
    item.write_text("movie")

    response = client.post(
        "/watches/downloads/rules/dry-run",
        data={"item": str(item), "rules": rules_path.read_text()},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200, response.text
    assert "Dry run" in response.text
    assert "No rule matched" in response.text


def test_rule_editor_dry_run_uses_unsaved_yaml(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    item = tmp_path / "downloads" / "movie.txt"
    item.write_text("movie")

    response = client.post(
        "/watches/downloads/rules/dry-run",
        data={
            "item": str(item),
            "rules": """rules:
  - name: Rename movie
    match: {field: file_name, pattern: '^movie\\.txt$'}
    actions:
      - rename: {name: preview.txt}
""",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "rename" in response.text
    assert "preview.txt" in response.text


def test_rule_editor_escapes_validation_feedback(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/watches/downloads/rules/validate",
        data={"rules": "rules:\n  - name: <script>alert(1)</script>\n"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_rule_editor_save_returns_conflict_feedback_without_overwriting(tmp_path: Path) -> None:
    client, rules_path, _ = make_client(tmp_path)
    original = rules_path.read_bytes()

    response = client.post(
        "/watches/downloads/rules/save",
        data={"rules": "rules: []\n", "expected_revision": "outdated"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 409
    assert "conflict" in response.text
    assert rules_path.read_bytes() == original


def test_attempt_and_log_pages_render_html(tmp_path: Path) -> None:
    client, rules_path, log_sink = make_client(tmp_path)
    item = tmp_path / "downloads" / "movie.txt"
    item.write_text("movie")
    log_sink.write(LogEntry.create(level=LogLevel.INFO, watch="downloads", rule="Archive", action="move", item=str(item), result=LogResult.OK))

    attempts = client.get("/attempts", headers={"accept": "text/html"})
    logs = client.get("/logs", headers={"accept": "text/html"})

    assert attempts.status_code == 200
    assert "Processing attempts" in attempts.text
    assert logs.status_code == 200
    assert "Log viewer" in logs.text
