from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import IO, Protocol, Sequence


class LogLevel(StrEnum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    DRYRUN = "DRYRUN"


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


class LogSink(Protocol):
    def write(self, entry: LogEntry) -> None: ...


class MemoryLogSink:
    def __init__(self, limit: int = 1000) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=limit)

    def write(self, entry: LogEntry) -> None:
        self._entries.append(entry)

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


class StructuredLogger:
    def __init__(self, sinks: Sequence[LogSink] = ()) -> None:
        self._sinks = list(sinks)

    def log(self, entry: LogEntry) -> None:
        for sink in self._sinks:
            sink.write(entry)
