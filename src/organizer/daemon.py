from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from organizer.config import OrganizerConfig, WatchFolderConfig
from organizer.item_processor import ExecutionMode, ItemProcessor, PlanRequest


class BatchProcessor(Protocol):
    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> None: ...


class ProcessorBatchAdapter:
    def __init__(self, processor: ItemProcessor) -> None:
        self.processor = processor

    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> None:
        for item in items:
            if not item.exists() or not item.is_relative_to(watch.watch_root):
                continue
            try:
                plan = self.processor.plan(PlanRequest(watch.watch_id, watch.watch_root, item, watch.rules_path, watch.boundary_policy))
                self.processor.execute(plan, ExecutionMode.APPLY)
            except (OSError, ValueError):
                continue


class WatcherService:
    def __init__(self, watches: tuple[WatchFolderConfig, ...], processor: BatchProcessor, debounce_seconds: float = 0.5) -> None:
        self._watches = {watch.watch_id: watch for watch in watches}
        self._processor = processor
        self._debounce_seconds = debounce_seconds
        self._pending: dict[tuple[str, Path], float] = {}
        self._observer: object | None = None
        self._lock = threading.Lock()

    def handle_event(self, watch_id: str, path: Path, event_type: str) -> None:
        if event_type not in {"created", "closed"} or watch_id not in self._watches:
            return
        watch = self._watches[watch_id]
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
            self._processor.process_batch(self._watches[watch_id], paths)
        return sum(len(paths) for paths in ready.values())

    def start(self) -> None:
        observer = Observer()
        handler = _WatchdogHandler(self)
        for watch in self._watches.values():
            if watch.watch_root.is_dir():
                observer.schedule(handler, str(watch.watch_root), recursive=True)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            observer = self._observer
            observer.stop()  # type: ignore[attr-defined]
            observer.join()  # type: ignore[attr-defined]
            self._observer = None
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
        for watch_id in self._service._watches:
            watch = self._service._watches[watch_id]
            if path.is_relative_to(watch.watch_root):
                self._service.handle_event(watch_id, path, event_type)
                return


class PeriodicScanner:
    def __init__(self, watches: tuple[WatchFolderConfig, ...], processor: BatchProcessor, interval_seconds: float = 300) -> None:
        self._watches = watches
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


@dataclass
class OrganizerDaemon:
    watches: tuple[WatchFolderConfig, ...]
    processor: BatchProcessor
    scanner_interval: float = 300

    def __post_init__(self) -> None:
        self.watcher = WatcherService(self.watches, self.processor)
        self.scanner = PeriodicScanner(self.watches, self.processor, self.scanner_interval)
        self._scanner_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    def start(self) -> None:
        self.watcher.start()
        self._scanner_task = asyncio.create_task(self.scanner.run())
        self._flush_task = asyncio.create_task(self._flush_watcher())

    async def _flush_watcher(self) -> None:
        while not self._stopped:
            self.watcher.flush()
            await asyncio.sleep(0.1)

    async def stop_async(self) -> None:
        self.watcher.stop()
        self.scanner.stop()
        if self._scanner_task is not None:
            await self._scanner_task
            self._scanner_task = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        self._stopped = True

    def stop(self) -> None:
        self.watcher.stop()
        self.scanner.stop()
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        self._stopped = True


def create_daemon(config: OrganizerConfig, processor: ItemProcessor) -> OrganizerDaemon:
    return OrganizerDaemon(config.watches, ProcessorBatchAdapter(processor), config.scan_interval)
