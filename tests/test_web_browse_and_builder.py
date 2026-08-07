from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from organizer.config import WatchFolderConfig
from organizer.item_processor import BoundaryPolicy, ItemProcessor
from organizer.web import _model_to_rules_yaml, _rules_yaml_to_model, create_app


def _make_client(tmp_path: Path) -> tuple[TestClient, Path]:
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
    config_path = tmp_path / "config" / "organizer.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"""data_roots:
  - {tmp_path}
config_root: {tmp_path / "config"}
watches:
  - id: downloads
    root: {watch_root}
    rules: rules.yaml
"""
    )
    app = create_app(processor, watch_folders=[config], config_path=config_path)
    return TestClient(app), rules_path


def _revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- model <-> YAML round trip ----


def test_model_to_yaml_and_back(tmp_path: Path) -> None:
    model = [
        {
            "name": "Videos",
            "allow_direct_deletion": False,
            "allow_hard_link_removal": False,
            "conditions": [
                {"name": "match", "field": "file_name", "pattern": r".*\.mp4$"},
                {"name": "year", "field": "full_path", "pattern": r"(2024)"},
            ],
            "actions": [
                {"kind": "move", "params": {"destination": "/data/videos"}},
                {"kind": "rename", "params": {"name": r"movie-\year.\\1.mp4"}},
            ],
        }
    ]
    yaml_text = _model_to_rules_yaml(model)
    assert "match:" in yaml_text
    assert "move:" in yaml_text
    assert "destination: /data/videos" in yaml_text
    parsed = _rules_yaml_to_model(yaml_text)
    assert parsed[0]["name"] == "Videos"
    assert parsed[0]["conditions"][0]["name"] == "match"
    assert parsed[0]["conditions"][1]["name"] == "year"
    assert parsed[0]["actions"][0] == {
        "kind": "move",
        "params": {"destination": "/data/videos"},
    }


def test_model_to_yaml_requires_name() -> None:
    import pytest

    with pytest.raises(ValueError):
        _model_to_rules_yaml([{"name": "", "conditions": [], "actions": []}])


def test_rules_yaml_to_model_handles_match_and_conditions(tmp_path: Path) -> None:
    rules = """rules:
  - name: R
    match: {field: file_name, pattern: '.*'}
    conditions:
      size: {field: full_path, pattern: 'x'}
    actions:
      - copy: {destination: /data/out}
"""
    model = _rules_yaml_to_model(rules)
    assert model[0]["name"] == "R"
    assert model[0]["conditions"][0]["name"] == "match"
    assert model[0]["conditions"][1]["name"] == "size"
    assert model[0]["actions"][0]["kind"] == "copy"


# ---- browse endpoints ----


def test_browse_lists_directories_only(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".hidden").mkdir()

    response = client.get("/browse/tree", params={"path": str(tmp_path)})

    assert response.status_code == 200
    body = response.json()
    names = [d["name"] for d in body["dirs"]]
    assert "alpha" in names
    assert "beta" in names
    assert "notes.txt" not in names
    assert ".hidden" not in names
    assert body["current"] == str(tmp_path)
    assert body["parent"] == ""


def test_browse_defaults_to_first_data_root(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    (tmp_path / "alpha").mkdir()

    response = client.get("/browse/tree")

    assert response.status_code == 200
    body = response.json()
    assert body["current_root"] == str(tmp_path)
    assert "alpha" in [d["name"] for d in body["dirs"]]


def test_browse_breadcrumb_and_parent(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    sub = tmp_path / "alpha" / "beta"
    sub.mkdir(parents=True)

    response = client.get("/browse/tree", params={"path": str(sub)})

    assert response.status_code == 200
    body = response.json()
    assert body["current"] == str(sub)
    assert body["relative"] == "alpha/beta"
    assert body["parent"] == str(tmp_path / "alpha")
    crumb_names = [c["name"] for c in body["crumb"]]
    assert crumb_names == [tmp_path.name, "alpha", "beta"]


def test_browse_tree_exposes_data_roots(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.get("/browse/tree")
    assert response.status_code == 200
    assert str(tmp_path) in response.json()["data_roots"]


def test_browse_rejects_path_outside_data_roots(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)

    response = client.get("/browse/tree", params={"path": str(outside)})

    assert response.status_code == 422
    assert "outside data volumes" in response.json()["error"]


def test_browse_rejects_file_path(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    a_file = tmp_path / "a.txt"
    a_file.write_text("x")

    response = client.get("/browse/tree", params={"path": str(a_file)})

    assert response.status_code == 422
    assert "not a directory" in response.json()["error"]


def test_browse_tree_no_data_roots_returns_400(tmp_path: Path) -> None:
    client = TestClient(create_app(ItemProcessor(tmp_path / "attempts.db")))
    response = client.get("/browse/tree")
    assert response.status_code == 400
    assert "no data roots" in response.json()["error"]


def test_browse_create_new_folder(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    response = client.post(
        "/browse/create",
        data={"path": str(tmp_path), "name": "newdir"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert (tmp_path / "newdir").is_dir()


def test_browse_create_rejects_bad_name(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)

    for bad in ["../escape", "a/b", ".", ".."]:
        response = client.post(
            "/browse/create",
            data={"path": str(tmp_path), "name": bad},
        )
        assert response.status_code == 422


def test_browse_create_rejects_parent_outside_data_roots(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)

    response = client.post(
        "/browse/create",
        data={"path": str(outside), "name": "x"},
    )

    assert response.status_code == 422
    assert "outside data volumes" in response.json()["error"]


# ---- rule builder endpoints ----


def _video_model(destination: Path) -> list[dict[str, object]]:
    return [
        {
            "name": "Videos",
            "allow_direct_deletion": False,
            "allow_hard_link_removal": False,
            "conditions": [
                {"name": "match", "field": "file_name", "pattern": r".*\.mp4$"}
            ],
            "actions": [
                {"kind": "move", "params": {"destination": str(destination)}}
            ],
        }
    ]


def test_rule_editor_renders_visual_builder(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.get("/watches/downloads/rules")
    assert response.status_code == 200
    assert "Visual builder" in response.text
    assert 'data-watch="downloads"' in response.text
    assert 'id="rules-yaml"' in response.text


def test_build_generate_returns_yaml(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.post(
        "/watches/downloads/rules/build/generate",
        json={"model": _video_model(tmp_path / "videos")},
    )
    assert response.status_code == 200
    assert "move:" in response.json()["yaml"]


def test_build_generate_rejects_bad_model(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.post(
        "/watches/downloads/rules/build/generate",
        json={"model": [{"name": "", "conditions": [], "actions": []}]},
    )
    assert response.status_code == 422
    assert "name" in response.json()["errors"][0]


def test_build_validate_reports_errors(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    model = _video_model(tmp_path / "videos")
    model[0]["actions"] = [{"kind": "bogus", "params": {}}]
    response = client.post(
        "/watches/downloads/rules/build/validate",
        json={"model": model},
    )
    assert response.status_code == 200
    assert response.json()["errors"]
    assert "unsupported action" in response.json()["errors"][0]


def test_build_validate_ok(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.post(
        "/watches/downloads/rules/build/validate",
        json={"model": _video_model(tmp_path / "videos")},
    )
    assert response.status_code == 200
    assert response.json()["errors"] == []


def test_build_save_persists(tmp_path: Path) -> None:
    client, rules_path = _make_client(tmp_path)
    response = client.post(
        "/watches/downloads/rules/build/save",
        json={
            "model": _video_model(tmp_path / "videos"),
            "expected_revision": _revision(rules_path),
        },
    )
    assert response.status_code == 200
    assert "Rules saved" in response.json()["message"]
    document = rules_path.read_text()
    assert "Videos" in document
    assert "move:" in document


def test_build_save_conflict_on_stale_revision(tmp_path: Path) -> None:
    client, rules_path = _make_client(tmp_path)
    response = client.post(
        "/watches/downloads/rules/build/save",
        json={
            "model": _video_model(tmp_path / "videos"),
            "expected_revision": "stale",
        },
    )
    assert response.status_code == 409
    assert "conflict" in response.json()["message"].lower()


def test_build_save_does_not_write_on_validation_error(tmp_path: Path) -> None:
    client, rules_path = _make_client(tmp_path)
    model = _video_model(tmp_path / "videos")
    model[0]["actions"] = [{"kind": "bogus", "params": {}}]
    response = client.post(
        "/watches/downloads/rules/build/save",
        json={
            "model": model,
            "expected_revision": _revision(rules_path),
        },
    )
    assert response.status_code == 422
    assert rules_path.read_text() == "rules: []\n"


def test_build_dry_run_reports_actions(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    target = tmp_path / "downloads" / "movie.mp4"
    target.write_text("x")

    response = client.post(
        "/watches/downloads/rules/build/dry-run",
        json={
            "model": _video_model(tmp_path / "videos"),
            "item": str(target),
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["actions"]
    assert data["actions"][0]["kind"] == "move"
    assert data["actions"][0]["source"] == str(target)


def test_build_dry_run_requires_item(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    response = client.post(
        "/watches/downloads/rules/build/dry-run",
        json={"model": _video_model(tmp_path / "videos"), "item": ""},
    )
    assert response.status_code == 422
    assert "item path" in response.json()["errors"][0]
