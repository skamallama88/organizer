from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from organizer.item_processor import ItemProcessor
from organizer.operational_health import OperationalHealth
from organizer.structured_log import LogEntry, LogLevel, LogResult, MemoryLogSink, StructuredLogger
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


def test_health_endpoint_returns_overall_status(tmp_path: Path) -> None:
    watch_root = tmp_path / "downloads"
    watch_root.mkdir()
    db_path = tmp_path / "attempts.db"
    health_checker = OperationalHealth()
    processor = ItemProcessor(db_path, health_checker=health_checker)
    app = create_app(
        processor,
        health_checker=health_checker,
        watch_folders=[("downloads", watch_root)],
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
        watch_folders=[("downloads", watch_root), ("broken", broken_root)],
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
        watch_folders=[("downloads", watch_root)],
        db_path=db_path,
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["all_healthy"] is False
    assert data["persistence_health"]["tracking_db_writable"] is False
