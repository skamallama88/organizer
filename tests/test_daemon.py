from __future__ import annotations

import asyncio
import io
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from organizer.config import WatchFolderConfig
from organizer.daemon import (
    DaemonHealthState,
    DaemonTask,
    DaemonWatchMutator,
    OrganizerDaemon,
    PeriodicScanner,
    ProcessorBatchAdapter,
    RetentionService,
    WatcherService,
    WatchMutator,
    effective_stability_interval,
    _filesystem_type,
    _is_inotify_supported,
)
from organizer.item_processor import (
    BatchItemStatus,
    BoundaryPolicy,
    DiscoveryBatch,
    ItemProcessor,
)
from organizer.retention import Retention
from organizer.structured_log import LogLevel, MemoryLogSink, StructuredLogger


class RecordingProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> DiscoveryBatch | None:
        self.calls.extend((watch.watch_id, item) for item in items)
        return None


class FlakyProcessor:
    """Succeeds after ``failures`` raising calls, then records calls normally."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, Path]] = []

    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> DiscoveryBatch | None:
        if self.failures > 0:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        self.calls.extend((watch.watch_id, item) for item in items)
        return None


class AlwaysFailProcessor:
    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> DiscoveryBatch | None:
        raise sqlite3.OperationalError("database is locked")


def watch(tmp_path: Path) -> WatchFolderConfig:
    root = tmp_path / "watch"
    root.mkdir()
    return WatchFolderConfig(
        watch_id="incoming",
        watch_root=root,
        rules_path=tmp_path / "rules.yaml",
        boundary_policy=BoundaryPolicy(),
    )


class RecordingObserver:
    def __init__(self) -> None:
        self.scheduled: list[tuple[object, str, bool]] = []
        self.unscheduled: list[object] = []

    def schedule(self, handler: object, path: str, recursive: bool) -> object:
        handle = object()
        self.scheduled.append((handler, path, recursive))
        return handle

    def unschedule(self, handle: object) -> None:
        self.unscheduled.append(handle)


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


def test_watcher_add_and_remove_watch_updates_observer_and_pending_events(tmp_path: Path) -> None:
    first = watch(tmp_path)
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = WatchFolderConfig(
        watch_id="second",
        watch_root=second_root,
        rules_path=tmp_path / "second-rules.yaml",
        boundary_policy=BoundaryPolicy(),
    )
    processor = RecordingProcessor()
    service = WatcherService((first,), processor, debounce_seconds=0)
    observer = RecordingObserver()
    service._observer = observer
    service.add_watch(second)
    handler = service._handler
    assert handler is not None

    event_path = second_root / "movie.mkv"
    service.handle_event("second", event_path, "created")
    service.remove_watch("second")

    assert observer.scheduled == [(handler, str(second_root), True)]
    assert len(observer.unscheduled) == 1
    assert service.flush() == 0
    assert "second" not in service._watches


def test_scanner_add_and_remove_watch_updates_watch_list(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    processor = RecordingProcessor()
    scanner = PeriodicScanner((), processor)

    scanner.add_watch(configured)
    scanner.remove_watch(configured.watch_id)

    assert scanner._watches == []


def test_watcher_ignores_events_for_disabled_watch(tmp_path: Path) -> None:
    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    disabled = WatchFolderConfig(
        watch_id="disabled",
        watch_root=disabled_root,
        rules_path=tmp_path / "disabled.yaml",
        boundary_policy=BoundaryPolicy(),
        enabled=False,
    )
    processor = RecordingProcessor()
    service = WatcherService((disabled,), processor, debounce_seconds=0)
    path = disabled_root / "movie.mkv"

    service.handle_event("disabled", path, "created")
    service.handle_event("disabled", path, "closed")

    assert service.flush() == 0
    assert processor.calls == []


def test_watcher_resumes_disabled_watch_after_update(tmp_path: Path) -> None:
    root = tmp_path / "watch"
    root.mkdir()
    configured = WatchFolderConfig(
        watch_id="incoming",
        watch_root=root,
        rules_path=tmp_path / "rules.yaml",
        boundary_policy=BoundaryPolicy(),
        enabled=False,
    )
    processor = RecordingProcessor()
    service = WatcherService((configured,), processor, debounce_seconds=0)
    path = root / "movie.mkv"

    service.update_watch(configured.model_copy(update={"enabled": True}))
    service.handle_event("incoming", path, "created")

    assert service.flush() == 1
    assert processor.calls == [("incoming", path)]


def test_daemon_mutator_cascades_and_rebuilds_boundary_policy(tmp_path: Path) -> None:
    first = watch(tmp_path)
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = WatchFolderConfig(
        watch_id="second",
        watch_root=second_root,
        rules_path=tmp_path / "second-rules.yaml",
        boundary_policy=BoundaryPolicy(),
    )
    daemon = OrganizerDaemon([first], RecordingProcessor(), scanner_interval=60)

    daemon.add_watch(second)

    assert [watch.watch_id for watch in daemon.watches] == [first.watch_id, second.watch_id]
    assert daemon.watcher._watches["second"].watch_root == second.watch_root
    assert [watch.watch_id for watch in daemon.scanner._watches] == [first.watch_id, second.watch_id]
    assert daemon.watches[0].boundary_policy.watch_roots == (first.watch_root, second_root)
    assert daemon.watches[1].boundary_policy.watch_roots == (first.watch_root, second_root)

    daemon.remove_watch(first.watch_id)

    assert [watch.watch_id for watch in daemon.watches] == [second.watch_id]
    assert "first" not in daemon.watcher._watches
    assert [watch.watch_id for watch in daemon.scanner._watches] == [second.watch_id]
    assert tuple(daemon.watches[0].boundary_policy.watch_roots) == (second_root,)


def test_daemon_watch_mutator_adapter_implements_protocol(tmp_path: Path) -> None:
    daemon = OrganizerDaemon([], RecordingProcessor(), scanner_interval=60)
    mutator: WatchMutator = DaemonWatchMutator(daemon)
    configured = watch(tmp_path)

    mutator.add_watch(configured)
    mutator.remove_watch(configured.watch_id)

    assert daemon.watches == []


def test_daemon_update_watch_replaces_runtime_config(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    daemon = OrganizerDaemon([configured], RecordingProcessor(), scanner_interval=60)
    updated = configured.model_copy(update={"enabled": False, "scan_interval": 600})

    daemon.update_watch(updated)

    assert daemon.watches[0].enabled is False
    assert daemon.watches[0].scan_interval == 600
    assert daemon.watcher._watches["incoming"].enabled is False
    assert daemon.scanner._watches[0].enabled is False


def test_daemon_trigger_scan_delegates_to_scanner(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    daemon = OrganizerDaemon([configured], RecordingProcessor(), scanner_interval=60)
    triggered: list[str] = []

    def _record(watch_id: str) -> None:
        triggered.append(watch_id)

    daemon.scanner.trigger = _record  # type: ignore[method-assign]

    daemon.trigger_scan("incoming")

    assert triggered == ["incoming"]


def test_scanner_offloads_batch_to_worker_thread(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    item = configured.watch_root / "movie.mkv"
    item.write_text("movie")
    loop_ref: list[asyncio.AbstractEventLoop] = []
    batch_thread: threading.Thread | None = None
    started = asyncio.Event()
    release = threading.Event()
    blocked_once = False

    class BlockingProcessor:
        def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> DiscoveryBatch | None:
            nonlocal batch_thread, blocked_once
            batch_thread = threading.current_thread()
            loop_ref[0].call_soon_threadsafe(started.set)
            if not blocked_once:
                blocked_once = True
                release.wait(timeout=5)
            return None

    scanner = PeriodicScanner((configured,), BlockingProcessor(), interval_seconds=0.01)

    async def run() -> None:
        loop_ref.append(asyncio.get_running_loop())
        task = asyncio.create_task(scanner.run())
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=1)
        responsive = True
        release.set()
        scanner.stop()
        await task
        return responsive

    responsive = asyncio.run(run())

    assert responsive
    assert batch_thread is not None
    assert batch_thread is not threading.main_thread()
    assert batch_thread is not loop_ref[0]


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


def test_scanner_uses_per_watch_interval_override(tmp_path: Path) -> None:
    fast_root = tmp_path / "fast"
    slow_root = tmp_path / "slow"
    fast_root.mkdir()
    slow_root.mkdir()
    fast = WatchFolderConfig(
        watch_id="fast",
        watch_root=fast_root,
        rules_path=tmp_path / "fast.yaml",
        boundary_policy=BoundaryPolicy(),
        scan_interval=1,
    )
    slow = WatchFolderConfig(
        watch_id="slow",
        watch_root=slow_root,
        rules_path=tmp_path / "slow.yaml",
        boundary_policy=BoundaryPolicy(),
        scan_interval=3600,
    )
    scanner = PeriodicScanner((fast, slow), RecordingProcessor(), interval_seconds=60)

    assert scanner._watch_interval(fast) == 1
    assert scanner._watch_interval(slow) == 3600

    now = time.monotonic()
    last_scans = {"fast": now - 2, "slow": now - 2}
    due = {w.watch_id for w in scanner._due_watches(last_scans)}
    assert due == {"fast"}

    last_scans = {"fast": now - 2, "slow": now - 3601}
    due = {w.watch_id for w in scanner._due_watches(last_scans)}
    assert due == {"fast", "slow"}


def test_scanner_scans_each_watch_once_on_startup(tmp_path: Path) -> None:
    fast_root = tmp_path / "fast"
    slow_root = tmp_path / "slow"
    fast_root.mkdir()
    slow_root.mkdir()
    fast_item = fast_root / "a.mkv"
    slow_item = slow_root / "b.mkv"
    fast_item.write_text("a")
    slow_item.write_text("b")
    fast = WatchFolderConfig(
        watch_id="fast",
        watch_root=fast_root,
        rules_path=tmp_path / "fast.yaml",
        boundary_policy=BoundaryPolicy(),
        scan_interval=3600,
    )
    slow = WatchFolderConfig(
        watch_id="slow",
        watch_root=slow_root,
        rules_path=tmp_path / "slow.yaml",
        boundary_policy=BoundaryPolicy(),
        scan_interval=3600,
    )
    processor = RecordingProcessor()
    scanner = PeriodicScanner((fast, slow), processor, interval_seconds=3600)

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.03)
        scanner.stop()
        await task

    asyncio.run(run())

    assert ("fast", fast_item) in processor.calls
    assert ("slow", slow_item) in processor.calls


def test_scanner_skips_disabled_watches(tmp_path: Path) -> None:
    enabled_root = tmp_path / "enabled"
    disabled_root = tmp_path / "disabled"
    enabled_root.mkdir()
    disabled_root.mkdir()
    enabled_item = enabled_root / "a.mkv"
    disabled_item = disabled_root / "b.mkv"
    enabled_item.write_text("a")
    disabled_item.write_text("b")
    enabled_config = WatchFolderConfig(
        watch_id="enabled",
        watch_root=enabled_root,
        rules_path=tmp_path / "enabled.yaml",
        boundary_policy=BoundaryPolicy(),
    )
    disabled_config = WatchFolderConfig(
        watch_id="disabled",
        watch_root=disabled_root,
        rules_path=tmp_path / "disabled.yaml",
        boundary_policy=BoundaryPolicy(),
        enabled=False,
    )
    processor = RecordingProcessor()
    scanner = PeriodicScanner((enabled_config, disabled_config), processor, interval_seconds=0.01)

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.025)
        scanner.stop()
        await task

    asyncio.run(run())

    assert ("enabled", enabled_item) in processor.calls
    assert ("disabled", disabled_item) not in processor.calls


def test_scanner_trigger_scans_watch_immediately(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_item = first_root / "a.mkv"
    second_item = second_root / "b.mkv"
    first_item.write_text("a")
    second_item.write_text("b")
    first = WatchFolderConfig(
        watch_id="first",
        watch_root=first_root,
        rules_path=tmp_path / "first.yaml",
        boundary_policy=BoundaryPolicy(),
        scan_interval=3600,
    )
    second = WatchFolderConfig(
        watch_id="second",
        watch_root=second_root,
        rules_path=tmp_path / "second.yaml",
        boundary_policy=BoundaryPolicy(),
        scan_interval=3600,
    )
    processor = RecordingProcessor()
    scanner = PeriodicScanner((first, second), processor, interval_seconds=3600)

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.02)
        scanner.trigger("second")
        await asyncio.sleep(0.05)
        scanner.stop()
        await task

    asyncio.run(run())

    assert ("second", second_item) in processor.calls


def test_scanner_trigger_scans_disabled_watch(tmp_path: Path) -> None:
    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    disabled_item = disabled_root / "b.mkv"
    disabled_item.write_text("b")
    disabled_config = WatchFolderConfig(
        watch_id="disabled",
        watch_root=disabled_root,
        rules_path=tmp_path / "disabled.yaml",
        boundary_policy=BoundaryPolicy(),
        scan_interval=3600,
        enabled=False,
    )
    processor = RecordingProcessor()
    scanner = PeriodicScanner((disabled_config,), processor, interval_seconds=3600)

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.02)
        scanner.trigger("disabled")
        await asyncio.sleep(0.05)
        scanner.stop()
        await task

    asyncio.run(run())

    assert ("disabled", disabled_item) in processor.calls


def test_scanner_survives_a_failed_batch_and_continues(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    item = configured.watch_root / "movie.mkv"
    item.write_text("movie")
    processor = FlakyProcessor(failures=1)
    scanner = PeriodicScanner((configured,), processor, interval_seconds=0.01)

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.035)
        scanner.stop()
        await task

    asyncio.run(run())

    assert processor.failures == 0
    assert processor.calls.count(("incoming", item)) >= 1


def test_scanner_failure_emits_structured_log_and_records_health(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    processor = AlwaysFailProcessor()
    sink = MemoryLogSink()
    logger = StructuredLogger(sinks=[sink], level=LogLevel.ERROR)
    health = DaemonHealthState()
    scanner = PeriodicScanner(
        (configured,),
        processor,
        interval_seconds=0.01,
        logger=logger,
        health=health,
    )

    async def run() -> None:
        task = asyncio.create_task(scanner.run())
        await asyncio.sleep(0.025)
        scanner.stop()
        await task

    asyncio.run(run())

    entries = sink.read_recent()
    assert len(entries) >= 1
    assert entries[0].level == LogLevel.ERROR
    assert entries[0].action == "scan"
    assert "database is locked" in entries[0].detail
    assert health.last_scan_error.startswith("OperationalError")
    assert health.scanner_alive is True


def test_daemon_health_surfaces_last_scan_error(tmp_path: Path) -> None:
    configured = watch(tmp_path)
    processor = AlwaysFailProcessor()
    daemon = OrganizerDaemon(
        [configured],
        processor,
        scanner_interval=0.01,
        logger=StructuredLogger(),
    )

    async def run() -> None:
        daemon.start()
        await asyncio.sleep(0.04)
        daemon.scanner.stop()
        await daemon._scanner_task

    asyncio.run(run())

    health = daemon.daemon_health()
    assert health.scanner_alive is True
    assert health.last_scan_error != ""
    assert "OperationalError" in health.last_scan_error


def test_daemon_respawns_crashed_task(tmp_path: Path) -> None:
    daemon = OrganizerDaemon([], RecordingProcessor(), scanner_interval=60)
    daemon._respawn_delay_seconds = 0.01
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")

    async def run() -> None:
        daemon._spawn(flaky, DaemonTask.SCANNER)
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert attempts >= 2


def test_flush_watcher_backs_off_on_repeated_failures(tmp_path: Path, monkeypatch: Any) -> None:
    configured = watch(tmp_path)
    daemon = OrganizerDaemon([configured], RecordingProcessor(), scanner_interval=60)

    def _always_fail() -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(daemon.watcher, "flush", _always_fail)
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)
        if len(recorded) >= 4:
            daemon._stopped = True

    monkeypatch.setattr("organizer.daemon.asyncio.sleep", fake_sleep)

    async def run() -> None:
        await daemon._flush_watcher()

    asyncio.run(run())

    assert recorded[:4] == [0.1, 0.2, 0.4, 0.8]


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


def test_filesystem_type_picks_longest_matching_mount(tmp_path: Path, monkeypatch: Any) -> None:
    mounts = "\n".join(
        [
            "/dev/sda1 / ext4 rw 0 0",
            "shfs /mnt/user fuse.shfs rw,nosuid,nodev,noatime 0 0",
            "shfs /mnt/user/dls fuse.shfs rw 0 0",
            "",
        ]
    )
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(mounts))

    assert _filesystem_type(Path("/mnt/user/dls")) == "fuse.shfs"
    assert _filesystem_type(Path("/mnt/user/dls/sub/folder")) == "fuse.shfs"
    assert _filesystem_type(Path("/etc/passwd")) == "ext4"


def test_inotify_supported_is_false_for_fuse_and_nfs(monkeypatch: Any) -> None:
    mounts = "\n".join(
        [
            "shfs /mnt/user fuse.shfs rw 0 0",
            "host:/export /mnt/nfs nfs rw 0 0",
            "",
        ]
    )
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(mounts))

    assert _is_inotify_supported(Path("/mnt/user/dls")) is False
    assert _is_inotify_supported(Path("/mnt/nfs/share")) is False
    assert _is_inotify_supported(Path("/etc")) is True


def test_inotify_supported_defaults_true_when_mounts_unreadable(tmp_path: Path, monkeypatch: Any) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no /proc/mounts")

    monkeypatch.setattr("builtins.open", _boom)

    assert _is_inotify_supported(tmp_path) is True


def test_effective_stability_interval_is_per_watch(tmp_path: Path, monkeypatch: Any) -> None:
    mounts = "\n".join(["shfs /mnt/user fuse.shfs rw 0 0", ""])
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(mounts))

    assert effective_stability_interval(tmp_path / "local", 5.0) == 0.0
    assert effective_stability_interval(Path("/mnt/user/dls"), 5.0) == 5.0
    assert effective_stability_interval(tmp_path / "local", 0.0) == 0.0


def test_processor_batch_adapter_defers_unstable_items_with_interval(tmp_path: Path, monkeypatch: Any) -> None:
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
    monkeypatch.setattr("organizer.daemon._is_inotify_supported", lambda _path: False)
    adapter = ProcessorBatchAdapter(processor, stability_interval=5.0)

    batch = adapter.process_batch(configured, [item])

    assert batch is not None
    assert batch.items[0].status == BatchItemStatus.DEFERRED
    assert item.exists()
    assert not (destination / "movie.mkv").exists()


def test_processor_batch_adapter_keeps_fast_path_on_inotify_roots(tmp_path: Path, monkeypatch: Any) -> None:
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
    monkeypatch.setattr("organizer.daemon._is_inotify_supported", lambda _path: True)
    adapter = ProcessorBatchAdapter(processor, stability_interval=5.0)

    batch = adapter.process_batch(configured, [item])

    assert batch is not None
    assert batch.items[0].status == "executed"
    assert (destination / "movie.mkv").read_text() == "movie"
