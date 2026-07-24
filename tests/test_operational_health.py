from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from organizer.operational_health import (
    OperationalHealth,
    PersistenceHealth,
    WatchFolderHealth,
)


def test_watch_folder_health_is_accessible_when_root_exists(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    health = OperationalHealth()

    result = health.check_watch_folder("downloads", watch_root)

    assert result.watch_id == "downloads"
    assert result.accessible is True
    assert result.detail == ""


def test_watch_folder_health_is_inaccessible_when_root_missing(tmp_path: Path) -> None:
    watch_root = tmp_path / "nonexistent"
    health = OperationalHealth()

    result = health.check_watch_folder("downloads", watch_root)

    assert result.accessible is False
    assert "does not exist" in result.detail or "not" in result.detail.lower()


def test_watch_folder_health_is_inaccessible_when_not_readable(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    watch_root.chmod(0o000)
    health = OperationalHealth()

    try:
        result = health.check_watch_folder("downloads", watch_root)
        assert result.accessible is False
    finally:
        watch_root.chmod(0o755)


def test_persistence_health_is_healthy_when_db_writable(tmp_path: Path) -> None:
    db_path = tmp_path / "organizer.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    health = OperationalHealth()

    result = health.check_persistence(db_path)

    assert result.tracking_db_writable is True
    assert result.detail == ""


def test_persistence_health_is_unhealthy_when_db_parent_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "nonexistent" / "organizer.db"
    health = OperationalHealth()

    result = health.check_persistence(db_path)

    assert result.tracking_db_writable is False
    assert result.detail != ""


def test_persistence_health_is_unhealthy_when_db_readonly(tmp_path: Path) -> None:
    db_path = tmp_path / "organizer.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE test (id INTEGER)")
    db_path.chmod(0o444)
    health = OperationalHealth()

    try:
        result = health.check_persistence(db_path)
        assert result.tracking_db_writable is False
    finally:
        db_path.chmod(0o644)


def test_persistence_health_check_uses_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "organizer.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE test (id INTEGER)")
    health = OperationalHealth()

    result = health.check_persistence(db_path)

    assert result.tracking_db_writable is True


def test_overall_health_is_healthy_when_all_checks_pass(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    db_path = tmp_path / "organizer.db"
    health = OperationalHealth()

    overall = health.check_all(
        watch_folders=[("downloads", watch_root)],
        db_path=db_path,
    )

    assert overall.all_healthy is True
    assert len(overall.watch_folder_healths) == 1
    assert overall.watch_folder_healths[0].accessible is True
    assert overall.persistence_health.tracking_db_writable is True


def test_overall_health_reports_unhealthy_watch_folder(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    broken_root = tmp_path / "broken"
    db_path = tmp_path / "organizer.db"
    health = OperationalHealth()

    overall = health.check_all(
        watch_folders=[("downloads", watch_root), ("broken", broken_root)],
        db_path=db_path,
    )

    assert overall.all_healthy is False
    broken_health = next(h for h in overall.watch_folder_healths if h.watch_id == "broken")
    assert broken_health.accessible is False


def test_overall_health_reports_unhealthy_persistence(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    db_path = tmp_path / "nonexistent" / "organizer.db"
    health = OperationalHealth()

    overall = health.check_all(
        watch_folders=[("downloads", watch_root)],
        db_path=db_path,
    )

    assert overall.all_healthy is False
    assert overall.persistence_health.tracking_db_writable is False


def test_watch_folder_health_only_affects_that_watch(tmp_path: Path) -> None:
    watch_a = tmp_path / "a"
    watch_a.mkdir()
    watch_b = tmp_path / "b"
    health = OperationalHealth()

    result_a = health.check_watch_folder("a", watch_a)
    result_b = health.check_watch_folder("b", watch_b)

    assert result_a.accessible is True
    assert result_b.accessible is False
