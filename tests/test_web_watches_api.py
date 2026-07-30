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

    def add_watch(self, watch: WatchFolderConfig) -> None:
        self.added.append(watch)

    def remove_watch(self, watch_id: str) -> None:
        self.removed.append(watch_id)


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


def test_post_watch_rejects_missing_rules_path(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post("/watches", json={"id": "test", "root": str(tmp_path)})

    assert response.status_code == 422
    assert "rules_path is required" in response.json()["detail"]


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
