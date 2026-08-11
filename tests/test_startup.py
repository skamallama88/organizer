from __future__ import annotations

import os
from pathlib import Path

from organizer.startup import apply_group_writable_umask, ensure_config_file


def test_ensure_config_file_creates_default_for_empty_config_volume(tmp_path: Path) -> None:
    config_path = tmp_path / "organizer.yaml"

    created = ensure_config_file(config_path)

    assert created is True
    assert config_path.read_text() == """# Organizer runtime configuration
data_roots:
  - /data
quarantine_root: /data/.quarantine
watches:
  - id: data
    root: /data
    rules: /config/rules.yaml
"""


def test_ensure_config_file_preserves_existing_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "organizer.yaml"
    config_path.write_text("watches: []\n")

    created = ensure_config_file(config_path)

    assert created is False
    assert config_path.read_text() == "watches: []\n"


def test_apply_group_writable_umask_sets_002(tmp_path: Path) -> None:
    previous = os.umask(0o022)

    apply_group_writable_umask()

    try:
        created = tmp_path / "outdir"
        created.mkdir()
        assert created.stat().st_mode & 0o777 == 0o775
    finally:
        os.umask(previous)
