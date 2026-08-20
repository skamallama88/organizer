from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Coroutine, Protocol, Sequence

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from organizer.config import (
    DiscoveryScope,
    OrganizerConfig,
    WatchFolderConfig,
    rebuild_boundary_policy,
)
from organizer.item_processor import (
    BatchProgress,
    DiscoveryBatch,
    ItemProcessor,
    ItemSnapshot,
)
from organizer.operational_health import DaemonTaskHealth
from organizer.retention import Retention
from organizer.structured_log import LogEntry, LogLevel, LogResult, StructuredLogger

logger = logging.getLogger(__name__)


class DaemonTask(StrEnum):
    """Named background tasks run by :class:`OrganizerDaemon`."""

    SCANNER = "scanner"
    FLUSH = "flush"
    RETENTION = "retention"


_TASK_ATTRS: dict[DaemonTask, str] = {
    DaemonTask.SCANNER: "_scanner_task",
    DaemonTask.FLUSH: "_flush_task",
    DaemonTask.RETENTION: "_retention_task",
}

_FLUSH_BACKOFF_BASE = 0.1
_FLUSH_BACKOFF_MAX = 30.0

_ORGANIZER_PREFIX = ".organizer-"


class DaemonHealthState:
    """Mutable liveness + recent-failure state for the daemon's background tasks.

    Read by ``OrganizerDaemon.daemon_health()`` which projects it into the
    immutable ``DaemonTaskHealth`` surfaced through ``/health``.
    """

    def __init__(self) -> None:
        self.scanner_alive = True
        self.last_scan_at = ""
        self.last_scan_error = ""
        self.crash_count = 0
        self.last_crash = ""

    def record_scan_ok(self) -> None:
        self.last_scan_at = datetime.now(timezone.utc).isoformat()
        self.last_scan_error = ""

    def record_scan_error(self, error: Exception) -> None:
        self.last_scan_error = f"{type(error).__name__}: {error}"

    def record_crash(self, name: DaemonTask, error: BaseException) -> None:
        self.crash_count += 1
        self.last_crash = f"{name.value} crashed: {type(error).__name__}: {error}"
        if name == DaemonTask.SCANNER:
            self.scanner_alive = False

    def record_respawn(self, name: DaemonTask) -> None:
        if name == DaemonTask.SCANNER:
            self.scanner_alive = True


@dataclass(frozen=True)
class ScanStatus:
    """Honest snapshot of the scanner's queue for one watch.

    Surfaced by ``POST /watches/{id}/scan`` so the response reflects whether a
    manual trigger was accepted, is already running, or is queued behind
    in-flight work.
    """

    watch_id: str
    batch_running: bool
    pending_triggers: int
    in_flight_batches: int


@dataclass(frozen=True)
class QueueStatus:
    """Daemon-global snapshot of queued scan/operate work.

    The counts are identical for every watch, so they live on a dedicated
    read rather than being obtained from one watch's ``ScanStatus``.
    """

    pending_triggers: int
    in_flight_batches: int


class BatchProcessor(Protocol):
    def process_batch(
        self,
        watch: WatchFolderConfig,
        items: list[Path],
        *,
        progress: Callable[[BatchProgress], None] | None = None,
    ) -> DiscoveryBatch | None: ...


class WatchMutator(Protocol):
    def add_watch(self, watch: WatchFolderConfig) -> None: ...

    def remove_watch(self, watch_id: str) -> None: ...

    def update_watch(self, watch: WatchFolderConfig) -> None: ...

    def trigger_scan(self, watch_id: str) -> None: ...

    def scan_status(self, watch_id: str) -> ScanStatus: ...

    def queue_status(self) -> QueueStatus: ...

    def batch_progress(self, watch_id: str) -> BatchProgress | None: ...


class ProcessorBatchAdapter:
    def __init__(self, processor: ItemProcessor, stability_interval: float = 0.0) -> None:
        self.processor = processor
        self._stability_interval = stability_interval

    def process_batch(
        self,
        watch: WatchFolderConfig,
        items: list[Path],
        *,
        progress: Callable[[BatchProgress], None] | None = None,
    ) -> DiscoveryBatch | None:
        snapshots: list[ItemSnapshot] = []
        for item in items:
            if not item.exists() or not item.is_relative_to(watch.watch_root):
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            snapshots.append(ItemSnapshot(path=item, size=stat.st_size, mtime=stat.st_mtime))
        if not snapshots:
            return None
        return self.processor.process_batch(
            watch_id=watch.watch_id,
            watch_root=watch.watch_root,
            rules_path=watch.rules_path,
            snapshots=snapshots,
            stability_interval=effective_stability_interval(watch.watch_root, self._stability_interval),
            boundary_policy=watch.boundary_policy,
            progress=progress,
        )


def _filesystem_type(path: Path) -> str | None:
    """Return the filesystem type backing ``path`` by scanning /proc/mounts.

    Returns ``None`` when it cannot be determined (e.g. not on Linux or the
    path is not under a recognised mount point). Uses the longest matching
    mount point so nested mounts resolve to their own type.
    """
    target = path.resolve()
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except OSError:
        return None
    best: tuple[int, str] | None = None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mount_point = parts[1]
        fs_type = parts[2]
        try:
            resolved = Path(mount_point).resolve()
        except OSError:
            continue
        try:
            target.relative_to(resolved)
        except ValueError:
            continue
        depth = len(resolved.parts)
        if best is None or depth > best[0]:
            best = (depth, fs_type)
    return best[1] if best is not None else None


def _is_inotify_supported(path: Path) -> bool:
    """Whether watchdog's inotify ``Observer`` works for ``path``.

    FUSE, NFS, CIFS/SMB, and 9p mounts do not deliver inotify events (and can
    block observer startup), so they fall back to the polling observer.
    Non-Linux platforms and unknown mounts default to inotify.
    """
    fs_type = _filesystem_type(path)
    if fs_type is None:
        return True
    return fs_type not in {
        "fuse",
        "fuse.shfs",
        "fuse.sshfs",
        "fuse.s3fs",
        "fuse.glusterfs",
        "nfs",
        "nfs4",
        "cifs",
        "smbfs",
        "9p",
    }


class WatcherService:
    def __init__(
        self,
        watches: Sequence[WatchFolderConfig],
        processor: BatchProcessor,
        debounce_seconds: float = 0.5,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._watches = {watch.watch_id: watch for watch in watches}
        self._processor = processor
        self._debounce_seconds = debounce_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._pending: dict[tuple[str, Path], float] = {}
        self._observer: object | None = None
        self._handler: _WatchdogHandler | None = None
        self._handles: dict[str, object] = {}
        self._lock = threading.Lock()

    def handle_event(self, watch_id: str, path: Path, event_type: str) -> None:
        if event_type not in {"created", "closed"}:
            return
        with self._lock:
            watch = self._watches.get(watch_id)
        if watch is None or not watch.enabled:
            return
        if not path.is_relative_to(watch.watch_root) or path.name.startswith(_ORGANIZER_PREFIX):
            return
        if watch.discovery is DiscoveryScope.TOP_LEVEL and path.parent != watch.watch_root:
            return
        with self._lock:
            self._pending[(watch_id, path)] = time.monotonic() + self._debounce_seconds

    def flush(self) -> int:
        now = time.monotonic()
        ready: dict[str, list[Path]] = {}
        with self._lock:
            for key, deadline in list(self._pending.items()):
                if deadline <= now or self._debounce_seconds == 0:
                    watch_id, path = key
                    ready.setdefault(watch_id, []).append(path)
                    del self._pending[key]
        for watch_id, paths in ready.items():
            with self._lock:
                watch = self._watches.get(watch_id)
            if watch is not None and watch.enabled:
                self._processor.process_batch(watch, paths)
        return sum(len(paths) for paths in ready.values())

    def start(self) -> None:
        with self._lock:
            watches = tuple(self._watches.values())
        inotify_supported = all(_is_inotify_supported(watch.watch_root) for watch in watches)
        observer: object = (
            Observer()
            if inotify_supported
            else PollingObserver(timeout=self._poll_interval_seconds)
        )
        handler = _WatchdogHandler(self)
        self._handler = handler
        for watch in watches:
            if watch.watch_root.is_dir():
                self._handles[watch.watch_id] = observer.schedule(handler, str(watch.watch_root), recursive=True)  # type: ignore[attr-defined]
        observer.start()  # type: ignore[attr-defined]
        self._observer = observer

    def add_watch(self, watch: WatchFolderConfig) -> None:
        with self._lock:
            self._watches[watch.watch_id] = watch
            observer = self._observer
            handler = self._handler or _WatchdogHandler(self)
            self._handler = handler
        if observer is not None and handler is not None and watch.watch_root.is_dir():
            handle = observer.schedule(handler, str(watch.watch_root), recursive=True)  # type: ignore[attr-defined]
            with self._lock:
                self._handles[watch.watch_id] = handle

    def remove_watch(self, watch_id: str) -> None:
        with self._lock:
            self._watches.pop(watch_id, None)
            self._pending = {key: deadline for key, deadline in self._pending.items() if key[0] != watch_id}
            handle = self._handles.pop(watch_id, None)
            observer = self._observer
        if handle is not None and observer is not None:
            observer.unschedule(handle)  # type: ignore[attr-defined]

    def update_watch(self, watch: WatchFolderConfig) -> None:
        with self._lock:
            if watch.watch_id in self._watches:
                self._watches[watch.watch_id] = watch

    def stop(self) -> None:
        if self._observer is not None:
            observer = self._observer
            observer.stop()  # type: ignore[attr-defined]
            observer.join()  # type: ignore[attr-defined]
            self._observer = None
            self._handler = None
            self._handles.clear()
        with self._lock:
            self._pending.clear()


class _WatchdogHandler(FileSystemEventHandler):
    def __init__(self, service: WatcherService) -> None:
        self._service = service

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._dispatch(event, "created")

    def on_closed(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._dispatch(event, "closed")

    def _dispatch(self, event: FileSystemEvent, event_type: str) -> None:
        path = Path(event.src_path.decode() if isinstance(event.src_path, bytes) else event.src_path)
        with self._service._lock:
            watches = tuple(self._service._watches.items())
        for watch_id, watch in watches:
            if path.is_relative_to(watch.watch_root):
                self._service.handle_event(watch_id, path, event_type)
                return


class PeriodicScanner:
    def __init__(
        self,
        watches: Sequence[WatchFolderConfig],
        processor: BatchProcessor,
        interval_seconds: float = 300,
        logger: StructuredLogger | None = None,
        health: DaemonHealthState | None = None,
    ) -> None:
        self._watches = list(watches)
        self._processor = processor
        self._interval_seconds = interval_seconds
        self._stop_event = asyncio.Event()
        self._trigger_event = asyncio.Event()
        self._pending_triggers: set[str] = set()
        self._in_flight: dict[str, asyncio.Task[None]] = {}
        self._progress: dict[str, BatchProgress] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._logger = logger
        self._health = health

    def _log_scan_failure(self, watch_id: str, error: Exception) -> None:
        if self._health is not None:
            self._health.record_scan_error(error)
        if self._logger is not None:
            self._logger.log(
                LogEntry.create(
                    level=LogLevel.ERROR,
                    watch=watch_id,
                    rule="",
                    action="scan",
                    item="",
                    result=LogResult.FAILED,
                    detail=f"scan failed: {type(error).__name__}: {error}",
                )
            )

    def trigger(self, watch_id: str) -> None:
        """Schedule an immediate scan of one watch root.

        Safe to call from any thread while the scanner is running. Triggers are
        coalesced per watch and processed on the next loop iteration.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._schedule_trigger, watch_id)

    def _schedule_trigger(self, watch_id: str) -> None:
        self._pending_triggers.add(watch_id)
        self._trigger_event.set()

    def _wake(self) -> None:
        """Wake the run loop so it recomputes due watches without scanning."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._trigger_event.set)

    def _watch_interval(self, watch: WatchFolderConfig) -> float:
        if watch.scan_interval is not None:
            return float(watch.scan_interval)
        return self._interval_seconds

    def _due_watches(self, last_scans: dict[str, float]) -> list[WatchFolderConfig]:
        now = time.monotonic()
        due: list[WatchFolderConfig] = []
        for watch in self._watches:
            if not watch.enabled:
                continue
            if watch.watch_id in self._in_flight:
                continue
            interval = self._watch_interval(watch)
            if now - last_scans.get(watch.watch_id, float("-inf")) >= interval:
                due.append(watch)
        return due

    def _find_watch(self, watch_id: str) -> WatchFolderConfig | None:
        for watch in self._watches:
            if watch.watch_id == watch_id:
                return watch
        return None

    def _timeout_until_next(self, last_scans: dict[str, float]) -> float:
        now = time.monotonic()
        best: float | None = None
        for watch in self._watches:
            if not watch.enabled:
                continue
            if watch.watch_id in self._in_flight:
                continue
            interval = self._watch_interval(watch)
            remaining = max(
                0.0, interval - (now - last_scans.get(watch.watch_id, float("-inf")))
            )
            if best is None or remaining < best:
                best = remaining
        if best is None:
            return self._interval_seconds
        return best

    async def _scan_watch(self, watch: WatchFolderConfig) -> None:
        def report(progress: BatchProgress) -> None:
            self._progress[watch.watch_id] = progress

        try:
            if watch.watch_root.is_dir():
                if watch.discovery is DiscoveryScope.TOP_LEVEL:
                    items = [path for path in watch.watch_root.iterdir()]
                else:
                    items = [
                        path
                        for path in watch.watch_root.rglob("*")
                        if not path.name.startswith(_ORGANIZER_PREFIX)
                    ]
                await asyncio.to_thread(
                    self._processor.process_batch, watch, items, progress=report
                )
                if self._health is not None:
                    self._health.record_scan_ok()
        except Exception as error:  # noqa: BLE001 — never let one batch kill the task
            self._log_scan_failure(watch.watch_id, error)

    def _launch_scan(self, watch: WatchFolderConfig, last_scans: dict[str, float]) -> None:
        """Start one watch's batch as an independent task.

        A per-watch in-flight guard prevents the same watch from being scanned
        concurrently with itself. Each batch runs to completion in its own task,
        so a long batch on one watch never blocks other watches' scans or queued
        manual triggers.
        """
        if watch.watch_id in self._in_flight:
            return
        last_scans[watch.watch_id] = time.monotonic()
        task = asyncio.create_task(self._scan_watch(watch))
        task.set_name(watch.watch_id)
        self._in_flight[watch.watch_id] = task
        task.add_done_callback(self._reap_finished)

    def _reap_finished(self, task: asyncio.Task[None]) -> None:
        """Drop a completed batch from the in-flight guard and wake the loop."""
        watch_id = task.get_name()
        self._in_flight.pop(watch_id, None)
        self._progress.pop(watch_id, None)
        if not task.cancelled():
            error = task.exception()
            if isinstance(error, Exception):
                self._log_scan_failure(watch_id, error)
        if not self._stop_event.is_set():
            self._trigger_event.set()

    async def run(self) -> None:
        """Periodically scan each enabled watch root for new items.

        Each watch is scanned when its own interval has elapsed since its last
        scan, falling back to the global interval when the watch has no
        per-watch override. Disabled watches are never scanned periodically, but
        an explicit manual ``trigger`` still scans the requested watch. Pending
        ``trigger`` requests are processed immediately on the next loop
        iteration, so a manual "scan now" does not wait for the interval.

        Batches run as independent tasks with a per-watch in-flight guard: a
        long-running batch on one watch does not block another watch's scheduled
        scan or a manual trigger, and no watch is ever scanned concurrently with
        itself. The per-batch ``try/except`` is the PRIMARY resilience guard:
        one bad batch (e.g. a transient ``database is locked``) is logged and
        the loop simply continues on the next tick. The task only dies if
        something escapes this loop body, which ``OrganizerDaemon._on_task_done``
        treats as a crash and respawns.
        """
        self._loop = asyncio.get_running_loop()
        last_scans: dict[str, float] = {}
        while not self._stop_event.is_set():
            # Manual triggers are serviced BEFORE scheduled scans so a "scan
            # now" is never starved by a sibling watch's long-running batch.
            pending, self._pending_triggers = self._pending_triggers, set()
            for watch_id in pending:
                triggered_watch = self._find_watch(watch_id)
                if triggered_watch is not None:
                    self._launch_scan(triggered_watch, last_scans)
            for watch in self._due_watches(last_scans):
                self._launch_scan(watch, last_scans)
            try:
                await asyncio.wait_for(
                    self._trigger_event.wait(),
                    timeout=self._timeout_until_next(last_scans),
                )
            except asyncio.TimeoutError:
                pass
            self._trigger_event.clear()

    def scan_status(self, watch_id: str) -> ScanStatus:
        """Snapshot the scanner's queue state for a watch.

        Safe to call from any thread: reads atomically-copied counts from the
        in-flight guard and pending trigger set.
        """
        return ScanStatus(
            watch_id=watch_id,
            batch_running=watch_id in self._in_flight,
            pending_triggers=len(self._pending_triggers),
            in_flight_batches=len(self._in_flight),
        )

    def queue_status(self) -> QueueStatus:
        """Daemon-global queue counts, identical for every watch.

        Safe to call from any thread: reads atomic ``len`` counts only.
        """
        return QueueStatus(
            pending_triggers=len(self._pending_triggers),
            in_flight_batches=len(self._in_flight),
        )

    def batch_progress(self, watch_id: str) -> BatchProgress | None:
        """The latest phase/progress snapshot for one in-flight batch.

        Safe to call from any thread: a single-key dict lookup written by the
        batch worker thread via atomic reference swap.
        """
        return self._progress.get(watch_id)

    def stop(self) -> None:
        self._stop_event.set()
        self._trigger_event.set()

    def add_watch(self, watch: WatchFolderConfig) -> None:
        self._watches[:] = [existing for existing in self._watches if existing.watch_id != watch.watch_id]
        self._watches.append(watch)
        self._wake()

    def remove_watch(self, watch_id: str) -> None:
        self._watches[:] = [watch for watch in self._watches if watch.watch_id != watch_id]
        self._wake()

    def update_watch(self, watch: WatchFolderConfig) -> None:
        for index, existing in enumerate(self._watches):
            if existing.watch_id == watch.watch_id:
                self._watches[index] = watch
                self._wake()
                return


class RetentionService:
    def __init__(self, retention: Retention, retention_days: int, interval_seconds: float = 3600) -> None:
        self._retention = retention
        self._retention_days = retention_days
        self._interval_seconds = interval_seconds
        self._stop_event = asyncio.Event()

    async def run(self, *, now: float | None = None) -> None:
        while not self._stop_event.is_set():
            self._retention.retention_run(self._retention_days, now=now)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()


@dataclass
class OrganizerDaemon:
    watches: list[WatchFolderConfig]
    processor: BatchProcessor
    scanner_interval: float = 300
    poll_interval_seconds: float = 1.0
    retention: RetentionService | None = None
    logger: StructuredLogger | None = None

    def __post_init__(self) -> None:
        self.watches = list(self.watches)
        self.health = DaemonHealthState()
        self.watcher = WatcherService(
            self.watches,
            self.processor,
            poll_interval_seconds=self.poll_interval_seconds,
        )
        self.scanner = PeriodicScanner(
            self.watches,
            self.processor,
            self.scanner_interval,
            logger=self.logger,
            health=self.health,
        )
        self._scanner_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._respawn_delay_seconds: float = 30

    def daemon_health(self) -> DaemonTaskHealth:
        return DaemonTaskHealth(
            scanner_alive=self.health.scanner_alive,
            last_scan_at=self.health.last_scan_at,
            last_scan_error=self.health.last_scan_error,
            crash_count=self.health.crash_count,
            last_crash=self.health.last_crash,
        )

    @property
    def stopped(self) -> bool:
        return self._stopped

    def add_watch(self, watch: WatchFolderConfig) -> None:
        self.watcher.remove_watch(watch.watch_id)
        self.scanner.remove_watch(watch.watch_id)
        self.watches[:] = [existing for existing in self.watches if existing.watch_id != watch.watch_id]
        self.watches.append(watch)
        self._rebuild_boundary_policy()
        updated = self.watches[-1]
        self.watcher.add_watch(updated)
        self.scanner.add_watch(updated)

    def remove_watch(self, watch_id: str) -> None:
        self.watcher.remove_watch(watch_id)
        self.scanner.remove_watch(watch_id)
        self.watches[:] = [watch for watch in self.watches if watch.watch_id != watch_id]
        self._rebuild_boundary_policy()

    def update_watch(self, watch: WatchFolderConfig) -> None:
        for index, existing in enumerate(self.watches):
            if existing.watch_id == watch.watch_id:
                self.watches[index] = watch
                break
        self._rebuild_boundary_policy()

    def trigger_scan(self, watch_id: str) -> None:
        """Request an immediate scan/operate cycle for one watch root."""
        self.scanner.trigger(watch_id)

    def scan_status(self, watch_id: str) -> ScanStatus:
        return self.scanner.scan_status(watch_id)

    def queue_status(self) -> QueueStatus:
        return self.scanner.queue_status()

    def batch_progress(self, watch_id: str) -> BatchProgress | None:
        return self.scanner.batch_progress(watch_id)

    def _rebuild_boundary_policy(self) -> None:
        rebuild_boundary_policy(self.watches)
        for watch in self.watches:
            self.watcher.update_watch(watch)
        for watch in self.watches:
            self.scanner.update_watch(watch)

    def start(self) -> None:
        self.watcher.start()
        self._scanner_task = self._spawn(self.scanner.run, DaemonTask.SCANNER)
        self._flush_task = self._spawn(self._flush_watcher, DaemonTask.FLUSH)
        if self.retention is not None:
            self._retention_task = self._spawn(self.retention.run, DaemonTask.RETENTION)

    def _spawn(self, coro_factory: Callable[[], Coroutine[Any, Any, None]], name: DaemonTask) -> asyncio.Task[None]:
        """Create a named task that logs and auto-respawns if it ever dies.

        This is a DEFENSIVE safety net. The loop bodies (scanner ``run`` and
        ``_flush_watcher``) already catch per-batch/per-flush failures, so a
        task normally exits cleanly on stop; this respawn only fires for a
        crash that escapes those hardened bodies.
        """
        task: asyncio.Task[None] = asyncio.create_task(coro_factory())
        task.set_name(name.value)
        task.add_done_callback(
            lambda t: self._on_task_done(t, name, coro_factory)
        )
        return task

    def _on_task_done(self, task: asyncio.Task[None], name: DaemonTask, coro_factory: Callable[[], Coroutine[Any, Any, None]]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._log_task_crash(name, error)
        if self._stopped:
            return

        def _respawn() -> None:
            if self._stopped:
                return
            self.health.record_respawn(name)
            replacement = self._spawn(coro_factory, name)
            self._remember_task(name, replacement)

        asyncio.get_running_loop().call_later(self._respawn_delay_seconds, _respawn)

    def _log_task_crash(self, name: DaemonTask, error: BaseException) -> None:
        self.health.record_crash(name, error)
        detail = f"daemon task {name.value} crashed: {type(error).__name__}: {error} — respawning in {self._respawn_delay_seconds:.0f}s"
        if self.logger is not None:
            self.logger.log(
                LogEntry.create(
                    level=LogLevel.ERROR,
                    watch="",
                    rule="",
                    action=f"daemon.{name.value}",
                    item="",
                    result=LogResult.FAILED,
                    detail=detail,
                )
            )
        else:
            logger.error("%s", detail)

    def _remember_task(self, name: DaemonTask, task: asyncio.Task[None]) -> None:
        attr = _TASK_ATTRS.get(name)
        if attr is not None:
            setattr(self, attr, task)

    def _log_flush_failure(self, error: Exception) -> None:
        detail = f"watcher flush failed: {type(error).__name__}: {error}"
        if self.logger is not None:
            self.logger.log(
                LogEntry.create(
                    level=LogLevel.ERROR,
                    watch="",
                    rule="",
                    action="flush",
                    item="",
                    result=LogResult.FAILED,
                    detail=detail,
                )
            )
        else:
            logger.error("%s", detail)

    async def _flush_watcher(self) -> None:
        consecutive_failures = 0
        while not self._stopped:
            try:
                await asyncio.to_thread(self.watcher.flush)
                consecutive_failures = 0
            except Exception as error:  # noqa: BLE001 — never let one flush kill the task
                consecutive_failures += 1
                self._log_flush_failure(error)
                backoff = min(_FLUSH_BACKOFF_MAX, _FLUSH_BACKOFF_BASE * (2 ** (consecutive_failures - 1)))
                await asyncio.sleep(backoff)
                continue
            await asyncio.sleep(0.1)

    async def stop_async(self) -> None:
        self.watcher.stop()
        self.scanner.stop()
        if self.retention is not None:
            self.retention.stop()
        if self._scanner_task is not None:
            await self._scanner_task
            self._scanner_task = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        if self._retention_task is not None:
            await self._retention_task
            self._retention_task = None
        self._stopped = True

    def stop(self) -> None:
        self.watcher.stop()
        self.scanner.stop()
        if self.retention is not None:
            self.retention.stop()
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        if self._retention_task is not None:
            self._retention_task.cancel()
            self._retention_task = None
        self._stopped = True


def create_daemon(
    config: OrganizerConfig,
    processor: ItemProcessor,
    retention_days: int | None = None,
    retention_interval: float | None = None,
    logger: StructuredLogger | None = None,
    poll_interval_seconds: float | None = None,
) -> OrganizerDaemon:
    retention_service: RetentionService | None = None
    if retention_days is not None and retention_days > 0:
        retention_service = RetentionService(
            Retention(processor._attempts_path, logger=processor._logger),
            retention_days=retention_days,
            interval_seconds=retention_interval or 3600,
        )
    return OrganizerDaemon(
        list(config.watches),
        ProcessorBatchAdapter(processor, float(config.stability_interval)),
        config.scan_interval,
        poll_interval_seconds=(
            float(config.poll_interval)
            if poll_interval_seconds is None
            else poll_interval_seconds
        ),
        retention=retention_service,
        logger=logger,
    )


def effective_stability_interval(watch_root: Path, stability_interval: float) -> float:
    """Stability gating interval for a single watch root.

    inotify-backed observers emit ``closed`` events, which already provide
    correct write-completion semantics, so they keep the fast path
    (``0.0``). Polling observers (FUSE/NFS/CIFS watch roots) never emit a
    close event, so the stability gate is the only reliable signal and the
    configured interval is applied.
    """
    if _is_inotify_supported(watch_root):
        return 0.0
    return stability_interval


class DaemonWatchMutator:
    def __init__(self, daemon: OrganizerDaemon) -> None:
        self._daemon = daemon

    def add_watch(self, watch: WatchFolderConfig) -> None:
        self._daemon.add_watch(watch)

    def remove_watch(self, watch_id: str) -> None:
        self._daemon.remove_watch(watch_id)

    def update_watch(self, watch: WatchFolderConfig) -> None:
        self._daemon.update_watch(watch)

    def trigger_scan(self, watch_id: str) -> None:
        self._daemon.trigger_scan(watch_id)

    def scan_status(self, watch_id: str) -> ScanStatus:
        return self._daemon.scan_status(watch_id)

    def queue_status(self) -> QueueStatus:
        return self._daemon.queue_status()

    def batch_progress(self, watch_id: str) -> BatchProgress | None:
        return self._daemon.batch_progress(watch_id)
