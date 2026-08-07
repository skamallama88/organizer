from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from organizer.config import OrganizerConfig, WatchFolderConfig, rebuild_boundary_policy
from organizer.item_processor import (
    DiscoveryBatch,
    ItemProcessor,
    ItemSnapshot,
)
from organizer.retention import Retention


class BatchProcessor(Protocol):
    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> DiscoveryBatch | None: ...


class WatchMutator(Protocol):
    def add_watch(self, watch: WatchFolderConfig) -> None: ...

    def remove_watch(self, watch_id: str) -> None: ...


class ProcessorBatchAdapter:
    def __init__(self, processor: ItemProcessor) -> None:
        self.processor = processor

    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> DiscoveryBatch | None:
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
            stability_interval=0.0,
            boundary_policy=watch.boundary_policy,
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
    def __init__(self, watches: Sequence[WatchFolderConfig], processor: BatchProcessor, debounce_seconds: float = 0.5) -> None:
        self._watches = {watch.watch_id: watch for watch in watches}
        self._processor = processor
        self._debounce_seconds = debounce_seconds
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
        if watch is None:
            return
        if not path.is_relative_to(watch.watch_root) or path.name.startswith(".organizer-"):
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
            if watch is not None:
                self._processor.process_batch(watch, paths)
        return sum(len(paths) for paths in ready.values())

    def start(self) -> None:
        with self._lock:
            watches = tuple(self._watches.values())
        inotify_supported = all(_is_inotify_supported(watch.watch_root) for watch in watches)
        observer: object = Observer() if inotify_supported else PollingObserver()
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
    def __init__(self, watches: Sequence[WatchFolderConfig], processor: BatchProcessor, interval_seconds: float = 300) -> None:
        self._watches = list(watches)
        self._processor = processor
        self._interval_seconds = interval_seconds
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        while not self._stop_event.is_set():
            for watch in self._watches:
                if watch.watch_root.is_dir():
                    items = [path for path in watch.watch_root.rglob("*") if not path.name.startswith(".organizer-")]
                    self._processor.process_batch(watch, items)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop_event.set()

    def add_watch(self, watch: WatchFolderConfig) -> None:
        self._watches[:] = [existing for existing in self._watches if existing.watch_id != watch.watch_id]
        self._watches.append(watch)

    def remove_watch(self, watch_id: str) -> None:
        self._watches[:] = [watch for watch in self._watches if watch.watch_id != watch_id]

    def update_watch(self, watch: WatchFolderConfig) -> None:
        for index, existing in enumerate(self._watches):
            if existing.watch_id == watch.watch_id:
                self._watches[index] = watch
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
    retention: RetentionService | None = None

    def __post_init__(self) -> None:
        self.watches = list(self.watches)
        self.watcher = WatcherService(self.watches, self.processor)
        self.scanner = PeriodicScanner(self.watches, self.processor, self.scanner_interval)
        self._scanner_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._retention_task: asyncio.Task[None] | None = None
        self._stopped = False

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

    def _rebuild_boundary_policy(self) -> None:
        rebuild_boundary_policy(self.watches)
        for watch in self.watches:
            self.watcher.update_watch(watch)
        for watch in self.watches:
            self.scanner.update_watch(watch)

    def start(self) -> None:
        self.watcher.start()
        self._scanner_task = asyncio.create_task(self.scanner.run())
        self._flush_task = asyncio.create_task(self._flush_watcher())
        if self.retention is not None:
            self._retention_task = asyncio.create_task(self.retention.run())

    async def _flush_watcher(self) -> None:
        while not self._stopped:
            self.watcher.flush()
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
        ProcessorBatchAdapter(processor),
        config.scan_interval,
        retention=retention_service,
    )


class DaemonWatchMutator:
    def __init__(self, daemon: OrganizerDaemon) -> None:
        self._daemon = daemon

    def add_watch(self, watch: WatchFolderConfig) -> None:
        self._daemon.add_watch(watch)

    def remove_watch(self, watch_id: str) -> None:
        self._daemon.remove_watch(watch_id)
