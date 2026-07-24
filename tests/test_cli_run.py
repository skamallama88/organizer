from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from organizer.config import OrganizerConfig, load_config
from organizer.daemon import ProcessorBatchAdapter, create_daemon
from organizer.item_processor import ItemProcessor
from organizer.operational_health import OperationalHealth
from organizer.structured_log import LogLevel, MemoryLogSink, RotatingFileLogSink, StdoutLogSink, StructuredLogger
from organizer.web import create_app


@pytest.fixture
def runtime_setup(tmp_path: Path) -> tuple[OrganizerConfig, MemoryLogSink, ItemProcessor, OperationalHealth]:
    watch_root = tmp_path / "watch"
    watch_root.mkdir(parents=True)
    destination = tmp_path / "sorted"
    destination.mkdir()
    config_path = tmp_path / "config" / "organizer.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"""
data_roots:
  - {watch_root.parent}
quarantine_root: {watch_root.parent / '.quarantine'}
scan_interval: 300
log_level: INFO
retention_days: 14
watches:
  - id: test
    root: {watch_root}
    rules: {config_path.parent / 'rules.yaml'}
""")
    rules_path = config_path.parent / "rules.yaml"
    rules_path.write_text(f"""
rules:
  - name: move
    match:
      field: file_name
      pattern: '.*'
    actions:
      - move:
          destination: {destination}
""")

    config = load_config(config_path)
    log_sink = MemoryLogSink(limit=1000)
    logger = StructuredLogger(
        sinks=[
            StdoutLogSink(),
            RotatingFileLogSink(config.log_path, retention_days=config.retention_days),
            log_sink,
        ],
        level=LogLevel(config.log_level),
    )
    health_checker = OperationalHealth()
    processor = ItemProcessor(
        attempts_path=config.database_path,
        logger=logger,
        health_checker=health_checker,
    )
    return config, log_sink, processor, health_checker


def test_run_creates_shared_components_from_config(
    runtime_setup: tuple[OrganizerConfig, MemoryLogSink, ItemProcessor, OperationalHealth],
) -> None:
    config, log_sink, processor, health_checker = runtime_setup
    daemon = create_daemon(config, processor)

    assert daemon is not None
    assert processor is not None
    assert health_checker is not None
    assert log_sink is not None

    assert config.scan_interval == 300
    assert config.log_level == "INFO"
    assert config.retention_days == 14
    assert len(config.watches) == 1
    assert config.watches[0].watch_id == "test"
    assert config.database_path is not None
    assert config.log_path is not None


def test_web_app_receives_same_shared_components(
    runtime_setup: tuple[OrganizerConfig, MemoryLogSink, ItemProcessor, OperationalHealth],
) -> None:
    config, log_sink, processor, health_checker = runtime_setup

    app = create_app(
        processor,
        log_sink=log_sink,
        health_checker=health_checker,
        watch_folders=config.watches,
        db_path=config.database_path,
    )

    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "test" in response.text


def test_combined_runtime_graceful_shutdown_stops_services(
    runtime_setup: tuple[OrganizerConfig, MemoryLogSink, ItemProcessor, OperationalHealth],
) -> None:
    config, log_sink, processor, health_checker = runtime_setup
    daemon = create_daemon(config, processor)

    async def run() -> None:
        daemon.start()
        assert not daemon.stopped
        await daemon.stop_async()
        assert daemon.stopped

    asyncio.run(run())


def test_combined_runtime_processes_item_and_records_attempt(
    runtime_setup: tuple[OrganizerConfig, MemoryLogSink, ItemProcessor, OperationalHealth],
) -> None:
    config, log_sink, processor, health_checker = runtime_setup

    app = create_app(
        processor,
        log_sink=log_sink,
        health_checker=health_checker,
        watch_folders=config.watches,
        db_path=config.database_path,
    )
    client = TestClient(app)
    daemon = create_daemon(config, processor)
    watch_root = config.watches[0].watch_root
    data_root = config.watches[0].boundary_policy.data_roots[0]
    destination = data_root / "sorted"

    async def run() -> None:
        daemon.start()
        try:
            item = watch_root / "test.txt"
            item.write_text("hello")

            adapter = ProcessorBatchAdapter(processor)
            batch = adapter.process_batch(config.watches[0], [item])
            assert batch is not None
            assert len(batch.items) == 1
            assert batch.items[0].status == "executed"
            assert batch.items[0].report is not None
            assert batch.items[0].report.status == "completed"

            assert (destination / "test.txt").read_text() == "hello"

            entries = log_sink.read_recent()
            assert any(e.watch == "test" for e in entries)

            assert config.log_path.exists()
            log_content = config.log_path.read_text()
            assert "move" in log_content

            response = client.get("/")
            assert response.status_code == 200
            assert "test" in response.text
        finally:
            await daemon.stop_async()

    asyncio.run(run())
