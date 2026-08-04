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


def test_dashboard_has_add_watch_button(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "Add Watch" in response.text
    assert 'hx-get="/watches/new"' in response.text


def test_dashboard_htmx_reload_returns_fragment_not_full_page(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    response = client.get("/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Add Watch" in response.text
    assert 'id="watch-list"' in response.text
    assert "<!doctype" not in response.text.lower()
    assert "<html" not in response.text.lower()
    assert "htmx.min.js" not in response.text
    assert 'id="dashboard"' not in response.text


def test_dashboard_has_remove_buttons(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert hx_delete in response.text
    assert hx_confirm in response.text


hx_delete = 'hx-delete="/watches/downloads"'
hx_confirm = "Remove watch 'downloads'?"


def test_dashboard_watch_list_is_updatable_via_target(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="watch-list"' in response.text


def test_watch_form_partial_renders(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    response = client.get("/watches/new")
    assert response.status_code == 200
    assert "Watch ID" in response.text
    assert "Folder to watch" in response.text
    assert "Rules path" in response.text
    assert 'hx-post="/watches"' in response.text
    assert 'name="folder"' in response.text
    assert "dirPicker()" in response.text
    assert "$dispatch('open-picker')" in response.text


def test_watch_form_root_dropdown_populated(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    response = client.get("/watches/new")
    assert response.status_code == 200
    assert str(tmp_path) in response.text


def test_watch_form_root_dropdown_uses_config_when_no_watches(tmp_path: Path) -> None:
    config_path = tmp_path / "organizer.yaml"
    config_path.write_text(f"data_roots:\n  - {tmp_path}\nconfig_root: {tmp_path / 'config'}\nwatches: []\n")
    client = TestClient(create_app(ItemProcessor(tmp_path / "attempts.db"), config_path=config_path))

    response = client.get("/watches/new")

    assert response.status_code == 200
    assert str(tmp_path) in response.text


def test_watch_form_has_cancel_button(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    response = client.get("/watches/new")
    assert response.status_code == 200
    assert "Cancel" in response.text
    assert 'hx-get="/"' in response.text
    assert 'hx-target="#dashboard"' in response.text


def test_add_watch_htmx_success_returns_watch_list(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "incoming_rules.yaml"
    new_rules.write_text("rules: []\n")

    response = client.post(
        "/watches",
        data={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "downloads" in response.text
    assert "incoming" in response.text
    assert "watch-list" in response.text or "<table" in response.text
    assert "Watch folders" in response.text


def test_add_watch_htmx_duplicate_id_shows_form_with_errors(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    watch_root = tmp_path / "downloads"

    response = client.post(
        "/watches",
        data={"id": "downloads", "root": str(watch_root), "rules_path": str(tmp_path / "r.yaml")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "duplicate" in response.text or "already" in response.text.lower()
    assert 'name="id"' in response.text
    assert "Could not add watch" in response.text


def test_add_watch_htmx_rejects_root_outside_data_roots(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)

    response = client.post(
        "/watches",
        data={"id": "outside", "root": str(outside), "rules_path": str(tmp_path / "r.yaml")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert "outside" in response.text.lower()


def test_remove_watch_htmx_success_returns_watch_list(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.delete("/watches/downloads", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "watch-list" in response.text or "<table" in response.text or "No watch folders" in response.text


def test_remove_watch_htmx_unknown_returns_error(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.delete("/watches/unknown", headers={"HX-Request": "true"})

    assert response.status_code == 404
    assert "not found" in response.text.lower()


def test_dashboard_renders_watch_health_rule_count_and_recent_activity(tmp_path: Path) -> None:
    client, rules_path, log_sink = make_client(tmp_path)
    rules_path.write_text("rules:\n  - name: Archive\n    match: {field: file_name, pattern: .+}\n    actions: [{move: {destination: ../archives}}]\n")
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
