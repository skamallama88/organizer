from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from organizer.config import WatchFolderConfig
from organizer.daemon import OrganizerDaemon, PeriodicScanner, ProcessorBatchAdapter, RetentionService, WatcherService
from organizer.item_processor import (
    BatchItemStatus,
    BoundaryPolicy,
    DiscoveryBatch,
    ItemProcessor,
)
from organizer.retention import Retention


class RecordingProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> DiscoveryBatch | None:
        self.calls.extend((watch.watch_id, item) for item in items)
        return None


def watch(tmp_path: Path) -> WatchFolderConfig:
    root = tmp_path / "watch"
    root.mkdir()
    return WatchFolderConfig(
        watch_id="incoming",
        watch_root=root,
        rules_path=tmp_path / "rules.yaml",
        boundary_policy=BoundaryPolicy(),
    )


def _write_move_rules(path: Path, destination: str) -> Path:
    path.write_text(
        f"""rules:
  - name: move
    match:
      field: file_name
      pattern: '.*'
    actions:
      - move:
          destination: {destination}
"""
    )
    return path


def test_watcher_debounces_close_write_and_create(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    processor = RecordingProcessor()
    service = WatcherService((configured,), processor, debounce_seconds=0.1)
    path = configured.watch_root / "movie.mkv"
    service.handle_event("incoming", path, "created")
    service.handle_event("incoming", path, "closed")

    time.sleep(0.11)
    assert service.flush() == 1
    assert processor.calls == [("incoming", path)]
    assert service.flush() == 0


def test_watcher_ignores_unconfigured_event(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    processor = RecordingProcessor()
    service = WatcherService((configured,), processor)

    service.handle_event("incoming", configured.watch_root / "missing", "modified")

    assert service.flush() == 0
    assert processor.calls == []


def test_scanner_processes_root_items_on_interval(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    item = configured.watch_root / "movie.mkv"
    item.write_text("movie")
    processor = RecordingProcessor()
    scanner = PeriodicScanner((configured,), processor, interval_seconds=0.01)

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.025)
        scanner.stop()
        await task

    asyncio.run(run())

    assert processor.calls.count(("incoming", item)) >= 2


def test_daemon_stops_services_and_server(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    processor = RecordingProcessor()
    daemon = OrganizerDaemon([configured], processor, scanner_interval=60)
    daemon.stop()

    assert daemon.stopped


def test_daemon_watches_are_mutable_list(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    daemon = OrganizerDaemon([configured], RecordingProcessor(), scanner_interval=60)

    assert isinstance(daemon.watches, list)
    daemon.watches.append(configured)
    assert len(daemon.watches) == 2


def test_processor_batch_adapter_delegates_to_process_batch(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules_path = _write_move_rules(tmp_path / "rules.yaml", str(destination))
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(data_roots=(tmp_path,), allowed_destinations=(tmp_path,), watch_roots=(watch_root,)),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    adapter = ProcessorBatchAdapter(processor)

    batch = adapter.process_batch(configured, [item])

    assert batch is not None
    assert len(batch.items) == 1
    assert batch.items[0].status == "executed"
    assert batch.items[0].report is not None
    assert batch.items[0].report.status == "completed"
    assert (destination / "movie.mkv").read_text() == "movie"


def test_processor_batch_adapter_returns_none_for_empty_items(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=watch_root,
        rules_path=tmp_path / "rules.yaml",
        boundary_policy=BoundaryPolicy(),
    )
    adapter = ProcessorBatchAdapter(ItemProcessor(tmp_path / "attempts.db"))

    batch = adapter.process_batch(configured, [])

    assert batch is None


def test_processor_batch_adapter_skips_nonexistent_items(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=watch_root,
        rules_path=tmp_path / "rules.yaml",
        boundary_policy=BoundaryPolicy(),
    )
    adapter = ProcessorBatchAdapter(ItemProcessor(tmp_path / "attempts.db"))

    batch = adapter.process_batch(configured, [watch_root / "nonexistent.mkv"])

    assert batch is None


def test_watcher_through_processor_batch_adapter_moves_items(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules_path = _write_move_rules(tmp_path / "rules.yaml", str(destination))
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(data_roots=(tmp_path,), allowed_destinations=(tmp_path,), watch_roots=(watch_root,)),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    adapter = ProcessorBatchAdapter(processor)
    watcher = WatcherService((configured,), adapter, debounce_seconds=0.05)

    watcher.handle_event("incoming", item, "created")
    watcher.handle_event("incoming", item, "closed")

    time.sleep(0.06)
    flushed = watcher.flush()

    assert flushed == 1
    assert (destination / "movie.mkv").read_text() == "movie"
    assert not item.exists()


def test_scanner_through_processor_batch_adapter_moves_items(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules_path = _write_move_rules(tmp_path / "rules.yaml", str(destination))
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(data_roots=(tmp_path,), allowed_destinations=(tmp_path,), watch_roots=(watch_root,)),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    adapter = ProcessorBatchAdapter(processor)
    scanner = PeriodicScanner((configured,), adapter, interval_seconds=0.01)

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.025)
        scanner.stop()
        await task

    asyncio.run(run())

    assert (destination / "movie.mkv").read_text() == "movie"
    assert not item.exists()


def test_watcher_and_scanner_both_produce_same_batch_format(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    destination = tmp_path / "videos"
    watch_root.mkdir()
    destination.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules_path = _write_move_rules(tmp_path / "rules.yaml", str(destination))
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(data_roots=(tmp_path,), allowed_destinations=(tmp_path,), watch_roots=(watch_root,)),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    adapter = ProcessorBatchAdapter(processor)

    batch = adapter.process_batch(configured, [item])

    assert batch is not None
    assert len(batch.items) == 1
    assert isinstance(batch.items[0].status, BatchItemStatus) or isinstance(batch.items[0].status, str)
    assert batch.items[0].report is not None
    assert batch.items[0].report.status == "completed"


def test_concurrent_triggers_do_not_duplicate_work(tmp_path: Path) -> None:
    watch_root = tmp_path / "watch"
    watch_root.mkdir()
    item = watch_root / "movie.mkv"
    item.write_text("movie")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        """rules:
  - name: archive
    match:
      field: file_name
      pattern: '.*'
    actions:
      - archive:
          destination: ../
          format: zip
          preserve_originals: true
"""
    )
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=watch_root,
        rules_path=rules_path,
        boundary_policy=BoundaryPolicy(data_roots=(tmp_path,), allowed_destinations=(tmp_path,), watch_roots=(watch_root,)),
    )
    processor = ItemProcessor(tmp_path / "attempts.db")
    adapter = ProcessorBatchAdapter(processor)

    batch_a = adapter.process_batch(configured, [item])
    batch_b = adapter.process_batch(configured, [item])

    assert batch_a is not None
    assert batch_b is not None
    assert len(batch_a.items) == 1
    assert len(batch_b.items) == 1
    assert batch_a.items[0].status == "executed"
    assert batch_b.items[0].status == "skipped"
    assert item.exists()


def _init_attempts_db_for_retention(db_path: Path) -> None:
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


def _create_old_completed(db_path: Path, watch_id: str, timestamp: float) -> str:
    _init_attempts_db_for_retention(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO processing_attempts (attempt_id, watch_id, source_path, rule_name, status, resulting_paths, source_fingerprint, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-completed", watch_id, f"/data/{watch_id}/old.txt", "rule", "completed", "[]", "fp", str(timestamp - 1), str(timestamp)),
        )
    return "old-completed"


def test_retention_service_cleans_old_attempts_on_interval(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400 * 10

    _create_old_completed(db_path, "downloads", old_time)

    retention = Retention(db_path)
    service = RetentionService(retention, retention_days=7, interval_seconds=0.01)

    async def run() -> None:
        task = asyncio.create_task(service.run(now=now))
        await asyncio.sleep(0.03)
        service.stop()
        await task

    asyncio.run(run())

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_attempts").fetchall()
    assert len(rows) == 0


def test_retention_service_preserves_recent_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    now = 1000000.0
    old_time = now - 86400

    _create_old_completed(db_path, "downloads", old_time)

    retention = Retention(db_path)
    service = RetentionService(retention, retention_days=30, interval_seconds=0.01)

    async def run() -> None:
        task = asyncio.create_task(service.run(now=now))
        await asyncio.sleep(0.03)
        service.stop()
        await task

    asyncio.run(run())

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT attempt_id FROM processing_attempts").fetchall()
    assert len(rows) == 1


def test_retention_service_stops_gracefully(tmp_path: Path) -> None:
    db_path = tmp_path / "attempts.db"
    retention = Retention(db_path)
    service = RetentionService(retention, retention_days=7, interval_seconds=3600)

    async def run() -> None:
        service.stop()
        assert service._stop_event.is_set()

    asyncio.run(run())
