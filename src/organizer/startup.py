from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG = """# Organizer runtime configuration
data_roots:
  - /data
quarantine_root: /data/.quarantine
watches:
  - id: data
    root: /data
    rules: /config/rules.yaml
"""


def ensure_config_file(config_path: Path = Path("/config/organizer.yaml")) -> bool:
    """Create the first-run configuration, without replacing administrator edits."""
    if config_path.exists():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config_path.open("x") as file:
            file.write(DEFAULT_CONFIG)
    except FileExistsError:
        return False
    return True


def apply_group_writable_umask() -> None:
    """Ensure app-created files and directories are group-writable.

    The Docker image runs as root with the inherited umask 022, so directories
    it creates are 755 (root-owned) and not writable by a non-root host user
    browsing the mounted volumes. A 002 umask yields 775 directories so group
    members can write into app-created output folders.
    """
    os.umask(0o002)
