from __future__ import annotations

from pathlib import Path

import pytest

from organizer.config import (
    ConfigError,
    load_config,
    validate_watch_id,
    validate_watch_root,
)


def test_validate_watch_id_rejects_duplicate_ids() -> None:
    with pytest.raises(ConfigError, match="duplicate watch id: downloads"):
        validate_watch_id("downloads", ["downloads"])


def test_validate_watch_id_rejects_case_insensitive_duplicate_ids() -> None:
    with pytest.raises(ConfigError, match="duplicate watch id: Downloads"):
        validate_watch_id("Downloads", ["downloads"])


def test_validate_watch_root_rejects_config_volume_and_overlaps(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config volume"):
        validate_watch_root(tmp_path / "config" / "watch", tmp_path / "config", [tmp_path], [])

    with pytest.raises(ConfigError, match="overlap"):
        validate_watch_root(tmp_path / "data" / "child", tmp_path / "config", [tmp_path / "data"], [tmp_path / "data"])


def test_load_config_resolves_global_and_watch_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "organizer.yaml"
    config_path.write_text(
        """scan_interval: 30
log_level: DEBUG
retention_days: 14
data_roots:
  - /data
quarantine_root: /data/.quarantine
watches:
  - id: downloads
    root: /data/downloads
    rules: rules/downloads.yaml
  - id: incoming
    root: /data/incoming
    rules: rules/incoming.yaml
"""
    )

    config = load_config(config_path)

    assert config.scan_interval == 30
    assert config.log_level == "DEBUG"
    assert config.retention_days == 14
    assert config.retention_interval == 3600
    assert config.watches[0].rules_path == (tmp_path / "rules/downloads.yaml").resolve()
    assert config.watches[0].boundary_policy.data_roots == (Path("/data"),)
    assert config.watches[0].boundary_policy.quarantine_root == Path("/data/.quarantine")
    assert config.watches[0].boundary_policy is config.watches[1].boundary_policy


def test_load_config_rejects_overlapping_watch_roots(tmp_path: Path) -> None:
    config_path = tmp_path / "organizer.yaml"
    config_path.write_text(
        """config_root: /config
data_roots: [/data]
watches:
  - id: parent
    root: /data
    rules: parent.yaml
  - id: child
    root: /data/child
    rules: child.yaml
"""
    )

    with pytest.raises(ConfigError, match="overlap"):
        load_config(config_path)


def test_load_config_rejects_config_volume_watch(tmp_path: Path) -> None:
    config_path = tmp_path / "organizer.yaml"
    config_path.write_text(
        """data_roots: [/data]
config_root: /config
watches:
  - id: bad
    root: /config/watch
    rules: rules.yaml
"""
    )

    with pytest.raises(ConfigError, match="config volume"):
        load_config(config_path)
