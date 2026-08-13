from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from organizer.config import WatchFolderConfig
from organizer.daemon import WatchMutator
from organizer.item_processor import BoundaryPolicy, ItemProcessor
from organizer.web import create_app


class RecordingMutator:
    def __init__(self) -> None:
        self.added: list[WatchFolderConfig] = []
        self.removed: list[str] = []
        self.updated: list[WatchFolderConfig] = []
        self.scanned: list[str] = []

    def add_watch(self, watch: WatchFolderConfig) -> None:
        self.added.append(watch)

    def remove_watch(self, watch_id: str) -> None:
        self.removed.append(watch_id)

    def update_watch(self, watch: WatchFolderConfig) -> None:
        self.updated.append(watch)

    def trigger_scan(self, watch_id: str) -> None:
        self.scanned.append(watch_id)


def _make_client(
    tmp_path: Path,
    *,
    mutator: WatchMutator | None = None,
    config_path: Path | None = None,
) -> tuple[TestClient, Path]:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules: []\n")
    processor = ItemProcessor(tmp_path / "attempts.db")
    config = WatchFolderConfig(
        watch_id="downloads",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(
            data_roots=(tmp_path,),
            config_root=tmp_path / "config",
            allowed_destinations=(tmp_path,),
            watch_roots=(watch_root,),
        ),
    )
    if config_path is None:
        config_path = tmp_path / "config" / "organizer.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f"""data_roots:
  - {tmp_path}
config_root: {tmp_path / "config"}
watches:
  - id: downloads
    root: {watch_root}
    rules: rules.yaml
""")
    app = create_app(
        processor,
        watch_folders=[config],
        watch_mutator=mutator,
        config_path=config_path,
    )
    return TestClient(app), config_path


def test_post_watch_adds_and_returns_config(tmp_path: Path) -> None:
    client, config_path = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "incoming_rules.yaml"
    new_rules.write_text("rules: []\n")

    response = client.post("/watches", json={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)})

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "incoming"
    assert data["root"] == str(new_root)
    assert data["rules_path"] == str(new_rules)


def test_post_watch_creates_rules_file_when_missing(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "config" / "rules_incoming.yaml"

    response = client.post(
        "/watches",
        json={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)},
    )

    assert response.status_code == 200
    assert new_rules.exists()
    assert new_rules.read_text() == "rules: []\n"


def test_post_watch_keeps_existing_rules_file(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "config" / "rules_incoming.yaml"
    new_rules.parent.mkdir(parents=True, exist_ok=True)
    new_rules.write_text(
        "rules:\n  - name: Existing\n    match: {field: file_name, pattern: .+}\n    actions: [{move: {destination: ../out}}]\n"
    )

    response = client.post(
        "/watches",
        json={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)},
    )

    assert response.status_code == 200
    assert new_rules.read_text().startswith("rules:\n  - name: Existing")


def test_post_watch_can_select_folder_under_root(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    root = tmp_path / "incoming"
    folder = root / "ready"
    folder.mkdir(parents=True)

    response = client.post(
        "/watches",
        json={
            "id": "ready",
            "root": str(root),
            "folder": "ready",
            "rules_path": str(tmp_path / "ready_rules.yaml"),
        },
    )

    assert response.status_code == 200
    assert response.json()["root"] == str(folder)


def test_post_watch_form_payload_accepts_default_rules_path(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    root = tmp_path / "incoming"
    root.mkdir()

    response = client.post(
        "/watches",
        data={
            "id": "incoming",
            "root": str(tmp_path),
            "folder": "incoming",
            "rules_path": str(tmp_path / "config" / "rules_incoming.yaml"),
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "incoming" in response.text


def test_post_watch_rejects_duplicate_id(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post("/watches", json={"id": "downloads", "root": str(tmp_path / "other"), "rules_path": str(tmp_path / "other.yaml")})

    assert response.status_code == 422
    assert "duplicate" in response.json()["detail"]


def test_post_watch_rejects_root_outside_data_roots(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)

    response = client.post("/watches", json={"id": "outside", "root": str(outside), "rules_path": str(tmp_path / "r.yaml")})

    assert response.status_code == 422
    assert "outside data volumes" in response.json()["detail"]


def test_post_watch_rejects_overlapping_root(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    overlapping = tmp_path / "downloads" / "sub"
    overlapping.mkdir()

    response = client.post("/watches", json={"id": "overlap", "root": str(overlapping), "rules_path": str(tmp_path / "r.yaml")})

    assert response.status_code == 422
    assert "overlap" in response.json()["detail"]


def test_post_watch_rejects_root_inside_config_volume(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    config_root = tmp_path / "config"
    config_root.mkdir(exist_ok=True)
    inside_config = config_root / "watch"
    inside_config.mkdir(exist_ok=True)

    response = client.post("/watches", json={"id": "bad", "root": str(inside_config), "rules_path": str(tmp_path / "r.yaml")})

    assert response.status_code == 422
    assert "config volume" in response.json()["detail"]


def test_post_watch_rejects_folder_dotdot_escape(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post(
        "/watches",
        json={"id": "esc", "root": str(tmp_path), "folder": "../../etc", "rules_path": str(tmp_path / "r.yaml")},
    )

    assert response.status_code == 422
    assert "may not contain '..'" in response.json()["detail"]


def test_post_watch_resolves_clean_folder_under_root(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    target = tmp_path / "ready"
    target.mkdir()

    response = client.post(
        "/watches",
        json={"id": "clean", "root": str(tmp_path), "folder": "ready", "rules_path": str(tmp_path / "r.yaml")},
    )

    assert response.status_code == 200
    assert response.json()["root"] == str(target.resolve())


def test_post_watch_rejects_bad_watch_id_charset(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()

    response = client.post(
        "/watches",
        json={"id": "../../etc", "root": str(new_root), "rules_path": str(tmp_path / "r.yaml")},
    )

    assert response.status_code == 422
    assert "invalid watch id" in response.json()["detail"]


def test_post_watch_rejects_watch_id_with_slash(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()

    response = client.post(
        "/watches",
        json={"id": "a/b", "root": str(new_root), "rules_path": str(tmp_path / "r.yaml")},
    )

    assert response.status_code == 422
    assert "invalid watch id" in response.json()["detail"]


def test_post_watch_rejects_missing_id(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post("/watches", json={"root": str(tmp_path), "rules_path": str(tmp_path / "r.yaml")})

    assert response.status_code == 422
    assert "id is required" in response.json()["detail"]


def test_post_watch_rejects_missing_root(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post("/watches", json={"id": "test", "rules_path": str(tmp_path / "r.yaml")})

    assert response.status_code == 422
    assert "root is required" in response.json()["detail"]


def test_post_watch_defaults_missing_rules_path(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()

    response = client.post("/watches", json={"id": "test", "root": str(new_root)})

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "test"
    assert data["rules_path"] == str(tmp_path / "config" / "rules_test.yaml")


def test_post_watch_rejects_invalid_json(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post("/watches", content=b"not json", headers={"content-type": "application/json"})

    assert response.status_code == 422


def test_post_watch_persists_to_yaml(tmp_path: Path) -> None:
    client, config_path = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "incoming_rules.yaml"
    new_rules.write_text("rules: []\n")

    client.post("/watches", json={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)})

    import yaml
    document = yaml.safe_load(config_path.read_text())
    ids = [w["id"] for w in document["watches"]]
    assert "incoming" in ids
    assert "downloads" in ids


def test_post_watch_calls_mutator(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, _ = _make_client(tmp_path, mutator=mutator)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "incoming_rules.yaml"
    new_rules.write_text("rules: []\n")

    client.post("/watches", json={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)})

    assert len(mutator.added) == 1
    assert mutator.added[0].watch_id == "incoming"
    assert mutator.added[0].watch_root == new_root


def test_post_watch_rebuilds_web_boundary_policy(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "incoming_rules.yaml"
    new_rules.write_text("rules: []\n")

    response = client.post(
        "/watches",
        json={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "incoming" in response.text
    assert response.text.lstrip().startswith("<h1>")
    assert "<!doctype" not in response.text.lower()
    assert "<html" not in response.text.lower()


def test_delete_watch_removes_and_returns_config(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.delete("/watches/downloads")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "downloads"
    assert data["status"] == "removed"


def test_delete_watch_returns_404_for_unknown(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.delete("/watches/unknown")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_delete_watch_calls_mutator(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, _ = _make_client(tmp_path, mutator=mutator)

    client.delete("/watches/downloads")

    assert mutator.removed == ["downloads"]


def test_delete_watch_removes_from_yaml(tmp_path: Path) -> None:
    client, config_path = _make_client(tmp_path)

    client.delete("/watches/downloads")

    import yaml
    document = yaml.safe_load(config_path.read_text())
    ids = [w["id"] for w in document["watches"]]
    assert "downloads" not in ids


def test_post_and_delete_round_trip(tmp_path: Path) -> None:
    client, config_path = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    new_rules = tmp_path / "incoming_rules.yaml"
    new_rules.write_text("rules: []\n")

    post_resp = client.post("/watches", json={"id": "incoming", "root": str(new_root), "rules_path": str(new_rules)})
    assert post_resp.status_code == 200

    import yaml
    document = yaml.safe_load(config_path.read_text())
    assert "incoming" in [w["id"] for w in document["watches"]]

    delete_resp = client.delete("/watches/incoming")
    assert delete_resp.status_code == 200

    document = yaml.safe_load(config_path.read_text())
    assert "incoming" not in [w["id"] for w in document["watches"]]


def test_delete_initially_unknown_then_added_watch(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "temp"
    new_root.mkdir()
    new_rules = tmp_path / "temp_rules.yaml"
    new_rules.write_text("rules: []\n")

    client.post("/watches", json={"id": "temp", "root": str(new_root), "rules_path": str(new_rules)})

    response = client.delete("/watches/temp")
    assert response.status_code == 200
    assert response.json()["id"] == "temp"


def test_post_watch_persistence_aborts_on_unreadable_config(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    new_root = tmp_path / "incoming"
    new_root.mkdir()
    bad = tmp_path / "config" / "organizer.yaml"
    bad.unlink()
    bad.mkdir()  # directory -> read_text raises OSError

    response = client.post("/watches", json={"id": "incoming", "root": str(new_root), "rules_path": str(tmp_path / "r.yaml")})

    assert response.status_code == 500
    assert "cannot read config" in response.json()["detail"]


def test_post_watch_without_config_path_returns_success_without_persistence(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules: []\n")
    processor = ItemProcessor(tmp_path / "attempts.db")
    config = WatchFolderConfig(
        watch_id="downloads",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(
            data_roots=(tmp_path,),
            config_root=tmp_path / "config",
            allowed_destinations=(tmp_path,),
            watch_roots=(watch_root,),
        ),
    )
    app = create_app(processor, watch_folders=[config])
    client = TestClient(app)
    new_root = tmp_path / "incoming"
    new_root.mkdir()

    response = client.post("/watches", json={"id": "incoming", "root": str(new_root), "rules_path": str(tmp_path / "r.yaml")})

    assert response.status_code == 200


def test_suppressions_list_and_clear(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules: []\n")
    processor = ItemProcessor(tmp_path / "attempts.db")
    source = watch_root / "bad.rar"
    processor._create_suppression("downloads", source, "fp123", "sup-1", "collision")
    config = WatchFolderConfig(
        watch_id="downloads",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(
            data_roots=(tmp_path,),
            config_root=tmp_path / "config",
            allowed_destinations=(tmp_path,),
            watch_roots=(watch_root,),
        ),
    )
    client = TestClient(create_app(processor, watch_folders=[config]))

    listed = client.get("/suppressions")

    assert listed.status_code == 200
    entries = listed.json()
    assert len(entries) == 1
    assert entries[0]["watch_id"] == "downloads"
    assert entries[0]["reason"] == "collision"
    assert entries[0]["source_path"] == str(source)
    assert "T" in entries[0]["suppressed_at"]

    cleared = client.post(
        "/suppressions/sup-1/clear",
        data={
            "watch_id": "downloads",
            "source_path": str(source),
            "source_fingerprint": "fp123",
        },
    )

    assert cleared.status_code == 200
    assert cleared.json()["success"] is True
    assert client.get("/suppressions").json() == []


def test_reprocess_attempt_endpoint_reruns_completed_attempt(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        """rules:
  - name: move
    match: {field: file_name, pattern: '.*'}
    actions:
      - move: {destination: ../videos}
"""
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    fingerprint = processor._fingerprint(item)
    import sqlite3
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("stale-completed", "downloads", str(item), "move", "completed", "[]", fingerprint, "1000.0"),
        )
    config = WatchFolderConfig(
        watch_id="downloads",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(
            data_roots=(tmp_path,),
            config_root=tmp_path / "config",
            allowed_destinations=(tmp_path,),
            watch_roots=(watch_root,),
        ),
    )
    client = TestClient(create_app(processor, watch_folders=[config]))

    response = client.post("/attempts/stale-completed/reprocess")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "completed"
    assert len(payload["actions"]) >= 1
    assert (destination / "movie.mkv").read_text() == "movie"


def test_reprocess_attempt_endpoint_returns_422_when_source_missing(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        """rules:
  - name: move
    match: {field: file_name, pattern: '.*'}
    actions:
      - move: {destination: ../videos}
"""
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    fingerprint = processor._fingerprint(item)
    import sqlite3
    with sqlite3.connect(tmp_path / "attempts.db") as conn:
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("stale-completed", "downloads", str(item), "move", "completed", "[]", fingerprint, "1000.0"),
        )
    item.unlink()
    config = WatchFolderConfig(
        watch_id="downloads",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(
            data_roots=(tmp_path,),
            config_root=tmp_path / "config",
            allowed_destinations=(tmp_path,),
            watch_roots=(watch_root,),
        ),
    )
    client = TestClient(create_app(processor, watch_folders=[config]))

    response = client.post("/attempts/stale-completed/reprocess")

    assert response.status_code == 422
    payload = response.json()
    assert "source no longer exists" in payload["detail"]
    assert "movie.mkv" in payload["detail"]


def test_patch_watch_disables_and_persists(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, config_path = _make_client(tmp_path, mutator=mutator)

    response = client.patch("/watches/downloads", data={"enabled": "false"})

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert len(mutator.updated) == 1
    assert mutator.updated[0].enabled is False
    import yaml
    document = yaml.safe_load(config_path.read_text())
    entry = [w for w in document["watches"] if w["id"] == "downloads"][0]
    assert entry["enabled"] is False


def test_patch_watch_sets_scan_interval_minutes(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, config_path = _make_client(tmp_path, mutator=mutator)

    response = client.patch("/watches/downloads", data={"scan_interval": "10"})

    assert response.status_code == 200
    assert response.json()["scan_interval"] == 600
    assert mutator.updated[0].scan_interval == 600
    import yaml
    document = yaml.safe_load(config_path.read_text())
    entry = [w for w in document["watches"] if w["id"] == "downloads"][0]
    assert entry["scan_interval"] == 600


def test_patch_watch_clears_scan_interval(tmp_path: Path) -> None:
    client, config_path = _make_client(tmp_path)
    client.patch("/watches/downloads", data={"scan_interval": "10"})
    client.patch("/watches/downloads", data={"scan_interval": ""})

    import yaml
    document = yaml.safe_load(config_path.read_text())
    entry = [w for w in document["watches"] if w["id"] == "downloads"][0]
    assert "scan_interval" not in entry


def test_patch_watch_rejects_invalid_scan_interval(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.patch("/watches/downloads", data={"scan_interval": "0"})

    assert response.status_code == 422
    assert "positive number of minutes" in response.json()["detail"]


def test_patch_watch_rejects_invalid_enabled_value(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.patch("/watches/downloads", data={"enabled": "banana"})

    assert response.status_code == 422
    assert "enabled must be true or false" in response.json()["detail"]


def test_patch_watch_returns_404_for_unknown(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.patch("/watches/unknown", data={"enabled": "true"})

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_patch_watch_htmx_returns_watch_list(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.patch(
        "/watches/downloads",
        data={"enabled": "false"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert 'id="watch-list"' in response.text
    assert "scan_interval" in response.text


def test_scan_now_triggers_via_mutator(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, _ = _make_client(tmp_path, mutator=mutator)

    response = client.post("/watches/downloads/scan")

    assert response.status_code == 200
    assert response.json()["detail"] == "Scan triggered for downloads."
    assert mutator.scanned == ["downloads"]


def test_scan_now_allowed_on_disabled_watch(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, _ = _make_client(tmp_path, mutator=mutator)
    client.patch("/watches/downloads", data={"enabled": "false"})

    response = client.post("/watches/downloads/scan")

    assert response.status_code == 200
    assert response.json()["detail"] == "Scan triggered for downloads."
    assert mutator.scanned == ["downloads"]


def test_scan_now_returns_404_for_unknown(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post("/watches/unknown/scan")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_scan_now_htmx_returns_feedback(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, _ = _make_client(tmp_path, mutator=mutator)

    response = client.post(
        "/watches/downloads/scan", headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert "Scan triggered for downloads." in response.text


def test_scan_now_without_mutator_returns_501(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path, mutator=None)

    response = client.post("/watches/downloads/scan")

    assert response.status_code == 501
    assert "no daemon running" in response.json()["detail"]


def test_scan_now_text_html_accept_returns_json(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, _ = _make_client(tmp_path, mutator=mutator)

    response = client.post(
        "/watches/downloads/scan",
        headers={"accept": "text/html"},
    )

    assert response.status_code == 200
    assert response.json() == {"watch_id": "downloads", "detail": "Scan triggered for downloads."}
    assert "<p" not in response.text
    assert mutator.scanned == ["downloads"]


def test_dashboard_lists_enabled_and_scan_interval(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert 'name="enabled"' in response.text
    assert 'name="scan_interval"' in response.text
    assert 'hx-post="/watches/downloads/scan"' in response.text


def test_dashboard_shows_interval_default_placeholder_and_override(tmp_path: Path) -> None:
    mutator = RecordingMutator()
    client, _ = _make_client(tmp_path, mutator=mutator)

    default_html = client.get("/").text
    assert 'placeholder="Default (5 min)"' in default_html

    client.patch("/watches/downloads", data={"scan_interval": "10"})

    override_html = client.get("/").text
    assert 'value="10"' in override_html
    assert 'placeholder="Default (5 min)"' in override_html
