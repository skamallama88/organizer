from __future__ import annotations

import io
from pathlib import Path

from organizer.structured_log import (
    LogEntry,
    LogLevel,
    LogResult,
    MemoryLogSink,
    RotatingFileLogSink,
    StdoutLogSink,
    StructuredLogger,
)


def test_log_entry_formats_as_structured_line() -> None:
    entry = LogEntry(
        timestamp="2024-01-01T00:00:00Z",
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
        detail="",
    )

    line = entry.format_line()

    assert "2024-01-01T00:00:00Z" in line
    assert "INFO" in line
    assert "downloads" in line
    assert "videos" in line
    assert "move" in line
    assert "/data/downloads/movie.mkv" in line
    assert "OK" in line


def test_log_entry_format_includes_detail_when_present() -> None:
    entry = LogEntry(
        timestamp="2024-01-01T00:00:00Z",
        level=LogLevel.ERROR,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.FAILED,
        detail="Destination already exists",
    )

    line = entry.format_line()

    assert "Destination already exists" in line


def test_memory_log_sink_stores_entries_up_to_limit() -> None:
    sink = MemoryLogSink(limit=3)

    for i in range(5):
        sink.write(LogEntry(
            timestamp=f"2024-01-01T00:00:0{i}Z",
            level=LogLevel.INFO,
            watch="downloads",
            rule="videos",
            action="move",
            item=f"/data/downloads/file{i}.mkv",
            result=LogResult.OK,
        ))

    entries = sink.read_recent()
    assert len(entries) == 3
    assert entries[0].item == "/data/downloads/file2.mkv"
    assert entries[-1].item == "/data/downloads/file4.mkv"


def test_memory_log_sink_filters_by_watch() -> None:
    sink = MemoryLogSink()
    sink.write(LogEntry(
        timestamp="2024-01-01T00:00:00Z",
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    ))
    sink.write(LogEntry(
        timestamp="2024-01-01T00:00:01Z",
        level=LogLevel.INFO,
        watch="inbox",
        rule="docs",
        action="copy",
        item="/data/inbox/doc.pdf",
        result=LogResult.OK,
    ))

    entries = sink.read_recent(watch="downloads")

    assert len(entries) == 1
    assert entries[0].watch == "downloads"


def test_memory_log_sink_filters_by_level() -> None:
    sink = MemoryLogSink()
    sink.write(LogEntry(
        timestamp="2024-01-01T00:00:00Z",
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    ))
    sink.write(LogEntry(
        timestamp="2024-01-01T00:00:01Z",
        level=LogLevel.ERROR,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/other.mkv",
        result=LogResult.FAILED,
        detail="collision",
    ))

    entries = sink.read_recent(level=LogLevel.ERROR)

    assert len(entries) == 1
    assert entries[0].level == LogLevel.ERROR


def test_memory_log_sink_default_limit_is_1000() -> None:
    sink = MemoryLogSink()

    for i in range(1500):
        sink.write(LogEntry(
            timestamp=f"ts-{i}",
            level=LogLevel.INFO,
            watch="w",
            rule="r",
            action="a",
            item=f"item-{i}",
            result=LogResult.OK,
        ))

    entries = sink.read_recent()
    assert len(entries) == 1000


def test_stdout_log_sink_writes_formatted_line() -> None:
    output = io.StringIO()
    sink = StdoutLogSink(stream=output)
    entry = LogEntry(
        timestamp="2024-01-01T00:00:00Z",
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    )

    sink.write(entry)

    output_text = output.getvalue()
    assert "downloads" in output_text
    assert "move" in output_text
    assert output_text.endswith("\n")


def test_structured_logger_writes_to_all_sinks() -> None:
    memory = MemoryLogSink()
    output = io.StringIO()
    stdout = StdoutLogSink(stream=output)
    logger = StructuredLogger(sinks=[memory, stdout])
    entry = LogEntry(
        timestamp="2024-01-01T00:00:00Z",
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    )

    logger.log(entry)

    assert len(memory.read_recent()) == 1
    assert "downloads" in output.getvalue()


def test_log_entry_timestamp_is_generated() -> None:
    entry = LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    )

    assert entry.timestamp != ""
    assert "T" in entry.timestamp


def test_log_entry_create_with_detail() -> None:
    entry = LogEntry.create(
        level=LogLevel.ERROR,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.FAILED,
        detail="collision detected",
    )

    assert entry.detail == "collision detected"
    assert entry.level == LogLevel.ERROR
    assert entry.result == LogResult.FAILED


def test_dry_run_log_level_is_dedicated() -> None:
    entry = LogEntry.create(
        level=LogLevel.DRYRUN,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.DRY_RUN,
    )

    assert entry.level == LogLevel.DRYRUN
    assert entry.result == LogResult.DRY_RUN


def test_rotating_file_sink_creates_log_file(tmp_path: Path) -> None:
    path = tmp_path / "organizer.log"
    sink = RotatingFileLogSink(path)
    entry = LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    )

    sink.write(entry)

    assert path.exists()
    assert "downloads" in path.read_text()


def test_rotating_file_sink_rotates_when_size_exceeded(tmp_path: Path) -> None:
    path = tmp_path / "organizer.log"
    sink = RotatingFileLogSink(path, max_size=1)
    entry = LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    )

    sink.write(entry)
    sink.write(entry)

    assert path.exists()
    backups = list(tmp_path.glob("organizer.log.*"))
    assert len(backups) >= 1


def test_rotating_file_sink_removes_old_backups(tmp_path: Path) -> None:
    path = tmp_path / "organizer.log"
    backup = tmp_path / "organizer.log.old"
    backup.write_text("old")
    old_mtime = 1.0
    import os
    os.utime(backup, (old_mtime, old_mtime))

    sink = RotatingFileLogSink(path, retention_days=1)
    entry = LogEntry.create(
        level=LogLevel.INFO,
        watch="downloads",
        rule="videos",
        action="move",
        item="/data/downloads/movie.mkv",
        result=LogResult.OK,
    )
    sink.write(entry)

    assert not backup.exists()
