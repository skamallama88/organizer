from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from organizer.retention import Retention


def _initialize_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS processing_attempts (
            attempt_id TEXT PRIMARY KEY,
            watch_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            status TEXT NOT NULL,
            resulting_paths TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            abandoned_reason TEXT NOT NULL DEFAULT '',
            failure_detail TEXT NOT NULL DEFAULT ''
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS processing_suppressions (
            watch_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            suppressed_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY (watch_id, source_path, source_fingerprint)
            )"""
        )


def _create_completed_attempt(db_path: Path, watch_id: str, completed_at: float) -> str:
    _initialize_db(db_path)
    with sqlite3.connect(db_path) as conn:
        attempt_id = f"completed-{completed_at}"
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, watch_id, f"/data/{watch_id}/file.txt", "rule", "completed", "[]", "fp", str(completed_at - 1), str(completed_at)),
        )
    return attempt_id


def _create_failed_attempt(db_path: Path, watch_id: str, completed_at: float) -> str:
    _initialize_db(db_path)
    with sqlite3.connect(db_path) as conn:
        attempt_id = f"failed-{completed_at}"
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint, failure_detail, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, watch_id, f"/data/{watch_id}/file.txt", "rule", "failed", "[]", "fp", "error", str(completed_at - 1), str(completed_at)),
        )
    return attempt_id


def _create_needs_reconciliation_attempt(db_path: Path, watch_id: str, completed_at: float) -> str:
    _initialize_db(db_path)
    with sqlite3.connect(db_path) as conn:
        attempt_id = f"reconcile-{completed_at}"
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, watch_id, f"/data/{watch_id}/file.txt", "rule", "needs-reconciliation", "[]", "fp", str(completed_at - 1), str(completed_at)),
        )
    return attempt_id


def _create_abandoned_attempt(db_path: Path, watch_id: str, completed_at: float) -> str:
    _initialize_db(db_path)
    with sqlite3.connect(db_path) as conn:
        attempt_id = f"abandoned-{completed_at}"
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint, abandoned_reason, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, watch_id, f"/data/{watch_id}/file.txt", "rule", "abandoned", "[]", "fp", "gave up", str(completed_at - 1), str(completed_at)),
        )
    return attempt_id


def _create_suppression(db_path: Path, watch_id: str, attempt_id: str) -> None:
    _initialize_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO processing_suppressions (watch_id, source_path, source_fingerprint, attempt_id, suppressed_at, reason) VALUES (?, ?, ?, ?, ?, ?)",
            (watch_id, f"/data/{watch_id}/file.txt", "fp", attempt_id, str(time.time()), "collision"),
        )


def test_retention_cleans_old_completed_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 10
    recent_time = now - 86400

    _create_completed_attempt(db_path, "downloads", old_time)
    _create_completed_attempt(db_path, "downloads", recent_time)

    retention = Retention(db_path)
    cleaned = retention.clean_completed(older_than=now - 86400 * 7)

    assert cleaned == 1
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_attempts").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == f"completed-{recent_time}"


def test_retention_cleans_old_failed_attempts_without_suppressions(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 10
    recent_time = now - 86400

    _create_failed_attempt(db_path, "downloads", old_time)
    _create_failed_attempt(db_path, "downloads", recent_time)

    retention = Retention(db_path)
    cleaned = retention.clean_failed(older_than=now - 86400 * 7)

    assert cleaned == 1
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_attempts").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == f"failed-{recent_time}"


def test_retention_does_not_clean_failed_attempts_with_suppressions(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 10

    old_attempt = _create_failed_attempt(db_path, "downloads", old_time)
    _create_suppression(db_path, "downloads", old_attempt)

    retention = Retention(db_path)
    cleaned = retention.clean_failed(older_than=now - 86400 * 7)

    assert cleaned == 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_attempts").fetchall()
    assert len(rows) == 1


def test_retention_never_cleans_needs_reconciliation_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 30

    _create_needs_reconciliation_attempt(db_path, "downloads", old_time)

    retention = Retention(db_path)
    cleaned_completed = retention.clean_completed(older_than=now - 86400 * 7)
    cleaned_failed = retention.clean_failed(older_than=now - 86400 * 7)

    assert cleaned_completed == 0
    assert cleaned_failed == 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_attempts").fetchall()
    assert len(rows) == 1


def test_retention_never_cleans_abandoned_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 30

    _create_abandoned_attempt(db_path, "downloads", old_time)

    retention = Retention(db_path)
    cleaned_completed = retention.clean_completed(older_than=now - 86400 * 7)
    cleaned_failed = retention.clean_failed(older_than=now - 86400 * 7)

    assert cleaned_completed == 0
    assert cleaned_failed == 0
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_attempts").fetchall()
    assert len(rows) == 1


def test_retention_never_cleans_suppressions(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 30

    old_attempt = _create_failed_attempt(db_path, "downloads", old_time)
    _create_suppression(db_path, "downloads", old_attempt)

    retention = Retention(db_path)
    retention.clean_failed(older_than=now - 86400 * 7)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_suppressions").fetchall()
    assert len(rows) == 1


def test_retention_clean_all_cleans_completed_and_failed_without_suppressions(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 10
    recent_time = now - 86400

    _create_completed_attempt(db_path, "downloads", old_time)
    _create_completed_attempt(db_path, "downloads", recent_time)
    _create_failed_attempt(db_path, "downloads", old_time)
    _create_needs_reconciliation_attempt(db_path, "downloads", old_time)
    _create_abandoned_attempt(db_path, "downloads", old_time)

    retention = Retention(db_path)
    cleaned = retention.clean_all(older_than=now - 86400 * 7)

    assert cleaned == 2
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id, status FROM processing_attempts").fetchall()
    statuses = {row[0]: row[1] for row in rows}
    assert f"completed-{recent_time}" in statuses
    assert statuses[f"completed-{recent_time}"] == "completed"
    assert any(status == "needs-reconciliation" for status in statuses.values())
    assert any(status == "abandoned" for status in statuses.values())


def test_retention_respects_watch_filter(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 10

    _create_completed_attempt(db_path, "downloads", old_time)
    _create_completed_attempt(db_path, "inbox", old_time + 1)

    retention = Retention(db_path)
    cleaned = retention.clean_completed(older_than=now - 86400 * 7, watch_id="downloads")

    assert cleaned == 1
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT watch_id FROM processing_attempts").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "inbox"
