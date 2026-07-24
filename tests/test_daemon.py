from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from organizer.config import WatchFolderConfig
from organizer.daemon import OrganizerDaemon, PeriodicScanner, WatcherService
from organizer.item_processor import BoundaryPolicy


class RecordingProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def process_batch(self, watch: WatchFolderConfig, items: list[Path]) -> None:
        self.calls.extend((watch.watch_id, item) for item in items)


def watch(tmp_path: Path) -> WatchFolderConfig:
    root = tmp_path / "watch"
    root.mkdir()
    return WatchFolderConfig(
        watch_id="incoming",
        watch_root=root,
        rules_path=tmp_path / "rules.yaml",
        boundary_policy=BoundaryPolicy(),
    )


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
    daemon = OrganizerDaemon((configured,), processor, scanner_interval=60)
    daemon.stop()

    assert daemon.stopped
