from __future__ import annotations

from pathlib import Path

import pytest

from organizer.config import ConfigError, load_config


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
