from __future__ import annotations

import os
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import IO, Protocol, Sequence


class LogLevel(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    DRYRUN = "DRYRUN"

    @property
    def priority(self) -> int:
        return {"INFO": 0, "WARN": 1, "ERROR": 2, "DRYRUN": -1}[self.value]


class LogResult(StrEnum):
    OK = "OK"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    DRY_RUN = "DRY_RUN"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    level: LogLevel
    watch: str
    rule: str
    action: str
    item: str
    result: LogResult
    detail: str = ""

    @classmethod
    def create(
        cls,
        *,
        level: LogLevel,
        watch: str,
        rule: str,
        action: str,
        item: str,
        result: LogResult,
        detail: str = "",
    ) -> LogEntry:
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            level=level,
            watch=watch,
            rule=rule,
            action=action,
            item=item,
            result=result,
            detail=detail,
        )

    def format_line(self) -> str:
        parts = [
            self.timestamp,
            self.level.value,
            self.watch,
            self.rule,
            self.action,
            self.item,
            self.result.value,
        ]
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


def parse_log_line(line: str) -> LogEntry | None:
    """Parse a line emitted by ``LogEntry.format_line`` back into a LogEntry.

    Returns None for empty or unparseable lines so callers can skip them.
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split(" | ", 7)
    if len(parts) < 7:
        return None
    timestamp, level, watch, rule, action, item, result = parts[:7]
    detail = parts[7] if len(parts) > 7 else ""
    try:
        return LogEntry(
            timestamp=timestamp,
            level=LogLevel(level),
            watch=watch,
            rule=rule,
            action=action,
            item=item,
            result=LogResult(result),
            detail=detail,
        )
    except ValueError:
        return None


class LogSink(Protocol):
    def write(self, entry: LogEntry) -> None: ...


class MemoryLogSink:
    def __init__(self, limit: int = 1000) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=limit)

    def write(self, entry: LogEntry) -> None:
        self._entries.append(entry)

    def hydrate(self, entries: Sequence[LogEntry]) -> None:
        """Seed the memory ring with prior entries (e.g. from a persisted file)."""
        for entry in entries:
            self.write(entry)

    def read_recent(
        self,
        limit: int | None = None,
        watch: str = "",
        level: LogLevel | None = None,
    ) -> list[LogEntry]:
        entries = list(self._entries)
        if watch:
            entries = [e for e in entries if e.watch == watch]
        if level is not None:
            entries = [e for e in entries if e.level == level]
        if limit is not None:
            entries = entries[-limit:]
        return entries


class StdoutLogSink:
    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream or sys.stdout

    def write(self, entry: LogEntry) -> None:
        self._stream.write(entry.format_line() + "\n")


class RotatingFileLogSink:
    def __init__(
        self,
        path: Path,
        *,
        max_size: int = 10 * 1024 * 1024,
        retention_days: int = 7,
        max_backup_files: int = 10,
    ) -> None:
        self._path = path
        self._max_size = max_size
        self._retention_days = retention_days
        self._max_backup_files = max_backup_files
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_files()

    def _backup_files(self) -> list[Path]:
        if not self._path.parent.exists():
            return []
        return sorted(
            p for p in self._path.parent.iterdir()
            if p != self._path and p.name.startswith(self._path.name)
        )

    def _cleanup_old_files(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        backups = self._backup_files()
        for backup in backups:
            try:
                mtime = datetime.fromtimestamp(backup.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    backup.unlink()
            except OSError:
                pass
        backups = self._backup_files()
        while len(backups) > self._max_backup_files:
            try:
                backups.pop(0).unlink()
            except OSError:
                break

    def _rotate(self) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = self._path.parent / f"{self._path.name}.{timestamp}"
        try:
            self._path.rename(backup)
        except OSError:
            pass
        self._cleanup_old_files()

    def write(self, entry: LogEntry) -> None:
        line = entry.format_line() + "\n"
        encoded = line.encode("utf-8")
        current_size = self._path.stat().st_size if self._path.exists() else 0
        if current_size + len(encoded) > self._max_size:
            self._rotate()
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def read_recent(self, limit: int = 1000) -> list[LogEntry]:
        """Read the last ``limit`` entries persisted by this sink."""
        if not self._path.exists():
            return []
        entries: list[LogEntry] = []
        with self._path.open("r", encoding="utf-8") as stream:
            for line in stream:
                entry = parse_log_line(line)
                if entry is not None:
                    entries.append(entry)
        return entries[-limit:]


class StructuredLogger:
    def __init__(self, sinks: Sequence[LogSink] = (), level: LogLevel = LogLevel.INFO) -> None:
        self._sinks = list(sinks)
        self._level = level

    def log(self, entry: LogEntry) -> None:
        if entry.level.priority >= self._level.priority:
            for sink in self._sinks:
                sink.write(entry)


def build_logger(
    *,
    log_path: Path,
    retention_days: int,
    memory_limit: int = 1000,
    level: LogLevel = LogLevel.INFO,
) -> tuple[StructuredLogger, MemoryLogSink]:
    """Build the shared application logger and its in-memory read-back ring.

    The memory ring is hydrated from the tail of the durable file so recent
    activity survives a process restart.
    """
    file_sink = RotatingFileLogSink(log_path, retention_days=retention_days)
    memory_sink = MemoryLogSink(limit=memory_limit)
    memory_sink.hydrate(file_sink.read_recent(memory_limit))
    logger = StructuredLogger(sinks=[StdoutLogSink(), file_sink, memory_sink], level=level)
    return logger, memory_sink
