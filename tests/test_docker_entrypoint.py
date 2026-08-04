from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"


def test_entrypoint_discovers_and_filters_mounts(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(
        """\
rootfs / rootfs rw 0 0
overlay / overlay rw 0 0
proc /proc proc rw 0 0
sysfs /sys sysfs rw 0 0
devtmpfs /dev devtmpfs rw 0 0
tmpfs /tmp tmpfs rw 0 0
/dev/sda /data ext4 rw 0 0
/dev/sdb /mnt/media ext4 rw 0 0
/dev/sdb /mnt/media ext4 rw 0 0
/dev/sdb /mnt/has\\134backslash ext4 rw 0 0
/dev/sdc /config ext4 rw 0 0
"""
    )

    env = {**os.environ, "CONFIG_DIR": str(config), "MOUNTS_FILE": str(mounts)}
    subprocess.run(["sh", str(ENTRYPOINT), "true"], env=env, check=True)

    generated = yaml.safe_load((config / "organizer.yaml").read_text())
    assert generated["data_roots"] == ["/data", "/mnt/media", "/mnt/has\\backslash"]
    assert generated["watches"] == [
        {"id": "data", "root": "/data", "rules": "/config/rules.yaml"}
    ]


def test_entrypoint_preserves_existing_configuration(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    original = "data_roots:\n  - /custom\n"
    (config / "organizer.yaml").write_text(original)
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/sda /mnt/media ext4 rw 0 0\n")

    env = {**os.environ, "CONFIG_DIR": str(config), "MOUNTS_FILE": str(mounts)}
    subprocess.run(["sh", str(ENTRYPOINT), "true"], env=env, check=True)

    assert (config / "organizer.yaml").read_text() == original
