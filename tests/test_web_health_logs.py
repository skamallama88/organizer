from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from organizer.item_processor import ItemProcessor
from organizer.operational_health import DaemonTaskHealth, OperationalHealth
from organizer.structured_log import LogEntry, LogLevel, LogResult, MemoryLogSink, StructuredLogger
from organizer.config import WatchFolderConfig
from organizer.web import create_app


def test_logs_endpoint_returns_recent_entries(tmp_path: Path) -> None:
    memory_sink = MemoryLogSink()
    logger = StructuredLogger(sinks=[memory_sink])
    processor = ItemProcessor(tmp_path / "attempts.db", logger=logger)
    app = create_app(processor, log_sink=memory_sink)
    client = TestClient(app)

    logger.log(LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    ))

    response = client.get("/logs")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["watch"] == "downloads"
    assert data[0]["rule"] == "videos"
    assert data[0]["action"] == "move"


def test_logs_endpoint_filters_by_watch(tmp_path: Path) -> None:
    memory_sink = MemoryLogSink()
    logger = StructuredLogger(sinks=[memory_sink])
    processor = ItemProcessor(tmp_path / "attempts.db", logger=logger)
    app = create_app(processor, log_sink=memory_sink)
    client = TestClient(app)

    logger.log(LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    ))
    logger.log(LogEntry.create(
        level=LogLevel.INFO,
        watch="inbox",
        rule="docs",
        action="copy",
        item="/data/inbox/doc.pdf",
        result=LogResult.OK,
    ))

    response = client.get("/logs", params={"watch": "downloads"})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["watch"] == "downloads"


def test_logs_endpoint_filters_by_level(tmp_path: Path) -> None:
    memory_sink = MemoryLogSink()
    logger = StructuredLogger(sinks=[memory_sink])
    processor = ItemProcessor(tmp_path / "attempts.db", logger=logger)
    app = create_app(processor, log_sink=memory_sink)
    client = TestClient(app)

    logger.log(LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    ))
    logger.log(LogEntry.create(
        level=LogLevel.ERROR,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/other.mkv",
        result=LogResult.FAILED,
        detail="collision",
    ))

    response = client.get("/logs", params={"level": "ERROR"})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["level"] == "ERROR"


def test_logs_endpoint_respects_limit(tmp_path: Path) -> None:
    memory_sink = MemoryLogSink()
    logger = StructuredLogger(sinks=[memory_sink])
    processor = ItemProcessor(tmp_path / "attempts.db", logger=logger)
    app = create_app(processor, log_sink=memory_sink)
    client = TestClient(app)

    for i in range(10):
        logger.log(LogEntry.create(
            level=LogLevel.INFO,
            watch="downloads",
            rule="videos",
            action="move",
            item=f"/data/downloads/file{i}.mkv",
            result=LogResult.OK,
        ))

    response = client.get("/logs", params={"limit": 5})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5


def test_logs_endpoint_rejects_invalid_level(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")
    client = TestClient(create_app(processor, log_sink=MemoryLogSink()))

    response = client.get("/logs", params={"level": "NOPE"})

    assert response.status_code == 422


def test_logs_page_renders_and_filters_date_range(tmp_path: Path) -> None:
    memory_sink = MemoryLogSink()
    processor = ItemProcessor(tmp_path / "attempts.db")
    app = create_app(processor, log_sink=memory_sink)
    client = TestClient(app)
    entry = LogEntry(
        timestamp=datetime(2026, 7, 24, tzinfo=timezone.utc).isoformat(),
        level=LogLevel.ERROR, watch="downloads", rule="videos", action="move",
        item="/data/movie.mkv", result=LogResult.FAILED, detail="collision",
    )
    memory_sink.write(entry)

    response = client.get("/logs", params={"level": "ERROR", "start": "2026-07-24", "end": "2026-07-24"}, headers={"accept": "text/html"})

    assert response.status_code == 200
    assert "Log viewer" in response.text
    assert "collision" in response.text
    assert 'name="start"' in response.text


def test_logs_page_excludes_entries_outside_date_range(tmp_path: Path) -> None:
    memory_sink = MemoryLogSink()
    processor = ItemProcessor(tmp_path / "attempts.db")
    app = create_app(processor, log_sink=memory_sink)
    client = TestClient(app)
    memory_sink.write(LogEntry(timestamp="2026-07-23T00:00:00+00:00", level=LogLevel.INFO, watch="downloads", rule="old", action="move", item="old", result=LogResult.OK))

    response = client.get("/logs", params={"start": "2026-07-24"}, headers={"accept": "text/html"})

    assert "old" not in response.text


def test_health_endpoint_returns_overall_status(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    db_path = tmp_path / "attempts.db"
    health_checker = OperationalHealth()
    processor = ItemProcessor(db_path, health_checker=health_checker)
    app = create_app(
        processor,
        health_checker=health_checker,
            watch_folders=[WatchFolderConfig(watch_id="downloads", watch_root=watch_root, rules_path=watch_root / "rules.yaml")],
        db_path=db_path,
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["all_healthy"] is True
    assert len(data["watch_folder_healths"]) == 1
    assert data["watch_folder_healths"][0]["watch_id"] == "downloads"
    assert data["watch_folder_healths"][0]["accessible"] is True
    assert data["persistence_health"]["tracking_db_writable"] is True


class FakeDaemonHealth:
    def __init__(self, daemon_health: DaemonTaskHealth) -> None:
        self._daemon_health = daemon_health

    def daemon_health(self) -> DaemonTaskHealth:
        return self._daemon_health


def test_health_endpoint_surfaces_daemon_task_state(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    db_path = tmp_path / "attempts.db"
    health_checker = OperationalHealth()
    processor = ItemProcessor(db_path, health_checker=health_checker)
    daemon_health = FakeDaemonHealth(
        DaemonTaskHealth(
            scanner_alive=True,
            last_scan_at="2026-08-11T22:00:00+00:00",
            last_scan_error="OperationalError: database is locked",
            crash_count=2,
            last_crash="scanner crashed: OperationalError: database is locked",
        )
    )
    app = create_app(
        processor,
        health_checker=health_checker,
        watch_folders=[WatchFolderConfig(watch_id="downloads", watch_root=watch_root, rules_path=watch_root / "rules.yaml")],
        db_path=db_path,
        daemon_health=daemon_health,
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["daemon_health"]["scanner_alive"] is True
    assert "database is locked" in data["daemon_health"]["last_scan_error"]
    assert data["daemon_health"]["crash_count"] == 2
    assert data["all_healthy"] is True


def test_health_endpoint_marks_unhealthy_when_scanner_dead(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    db_path = tmp_path / "attempts.db"
    health_checker = OperationalHealth()
    processor = ItemProcessor(db_path, health_checker=health_checker)
    daemon_health = FakeDaemonHealth(DaemonTaskHealth(scanner_alive=False))
    app = create_app(
        processor,
        health_checker=health_checker,
        watch_folders=[WatchFolderConfig(watch_id="downloads", watch_root=watch_root, rules_path=watch_root / "rules.yaml")],
        db_path=db_path,
        daemon_health=daemon_health,
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["daemon_health"]["scanner_alive"] is False
    assert data["all_healthy"] is False


def test_health_endpoint_reports_unhealthy_watch_folder(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    broken_root = tmp_path / "broken"
    db_path = tmp_path / "attempts.db"
    health_checker = OperationalHealth()
    processor = ItemProcessor(db_path, health_checker=health_checker)
    app = create_app(
        processor,
        health_checker=health_checker,
        watch_folders=[
            WatchFolderConfig(watch_id="downloads", watch_root=watch_root, rules_path=watch_root / "rules.yaml"),
            WatchFolderConfig(watch_id="broken", watch_root=broken_root, rules_path=broken_root / "rules.yaml"),
        ],
        db_path=db_path,
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["all_healthy"] is False
    broken_health = next(h for h in data["watch_folder_healths"] if h["watch_id"] == "broken")
    assert broken_health["accessible"] is False


def test_health_endpoint_reports_unhealthy_persistence(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    db_path = tmp_path / "nonexistent" / "attempts.db"
    health_checker = OperationalHealth()
    processor = ItemProcessor(tmp_path / "attempts.db", health_checker=health_checker)
    app = create_app(
        processor,
        health_checker=health_checker,
        watch_folders=[WatchFolderConfig(watch_id="downloads", watch_root=watch_root, rules_path=watch_root / "rules.yaml")],
        db_path=db_path,
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["all_healthy"] is False
    assert data["persistence_health"]["tracking_db_writable"] is False


def test_unknown_watch_is_rejected_by_dry_run(tmp_path: Path) -> None:
    processor = ItemProcessor(tmp_path / "attempts.db")
    response = TestClient(create_app(processor)).get(
        "/watches/missing/dry-run", params={"item": tmp_path / "item"}
    )

    assert response.status_code == 404
