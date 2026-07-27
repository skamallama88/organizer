from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from organizer.structured_log import LogEntry, LogLevel, LogResult, StructuredLogger


class Retention:
    def __init__(self, db_path: Path, logger: StructuredLogger | None = None) -> None:
        self._db_path = db_path
        self._logger = logger

    def _log(
        self,
        *,
        level: LogLevel,
        watch: str = "",
        action: str = "retention",
        item: str = "",
        result: LogResult,
        detail: str = "",
    ) -> None:
        if self._logger is not None:
            self._logger.log(
                LogEntry.create(
                    level=level,
                    watch=watch,
                    rule="",
                    action=action,
                    item=item,
                    result=result,
                    detail=detail,
                )
            )

    def clean_completed(self, older_than: float, watch_id: str = "") -> int:
        query = "DELETE FROM processing_attempts WHERE status = ? AND completed_at != '' AND CAST(completed_at AS REAL) < ?"
        params: list[object] = ["completed", older_than]
        if watch_id:
            query += " AND watch_id = ?"
            params.append(watch_id)
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(query, params)
            count = cursor.rowcount
        if count:
            self._log(level=LogLevel.INFO, item="processing_attempts", result=LogResult.OK, detail=f"cleaned {count} completed attempts")
        return count

    def clean_failed(self, older_than: float, watch_id: str = "") -> int:
        query = """
            DELETE FROM processing_attempts 
            WHERE status = ? 
            AND completed_at != '' 
            AND CAST(completed_at AS REAL) < ?
            AND attempt_id NOT IN (
                SELECT attempt_id FROM processing_suppressions
            )
        """
        params: list[object] = ["failed", older_than]
        if watch_id:
            query += " AND watch_id = ?"
            params.append(watch_id)
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(query, params)
            count = cursor.rowcount
        if count:
            self._log(level=LogLevel.INFO, item="processing_attempts", result=LogResult.OK, detail=f"cleaned {count} failed attempts")
        return count

    def clean_all(self, older_than: float, watch_id: str = "") -> int:
        completed = self.clean_completed(older_than, watch_id)
        failed = self.clean_failed(older_than, watch_id)
        return completed + failed

    def clean_staging_artifacts(self, older_than: float) -> int:
        staging_root = self._db_path.parent / "staging"
        if not staging_root.is_dir():
            return 0
        count = 0
        for entry in list(staging_root.iterdir()):
            if not entry.name.startswith(".organizer-staging-"):
                continue
            try:
                stat = entry.stat()
                mtime = stat.st_mtime
                if mtime < older_than:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                    count += 1
            except OSError:
                continue
        if count:
            self._log(level=LogLevel.INFO, item="staging", result=LogResult.OK, detail=f"cleaned {count} staging artifacts")
        return count

    def retention_run(self, retention_days: int, *, now: float | None = None) -> dict[str, int | list[str]]:
        _now = now if now is not None else time.time()
        older_than = _now - retention_days * 86400
        completed = 0
        failed = 0
        staging = 0
        errors: list[str] = []
        try:
            completed = self.clean_completed(older_than=older_than)
        except Exception as error:
            errors.append(f"completed: {error}")
        try:
            failed = self.clean_failed(older_than=older_than)
        except Exception as error:
            errors.append(f"failed: {error}")
        try:
            staging = self.clean_staging_artifacts(older_than=older_than)
        except Exception as error:
            errors.append(f"staging: {error}")
        total = completed + failed + staging
        if errors:
            detail = "; ".join(errors)
            self._log(
                level=LogLevel.ERROR,
                watch="",
                action="retention",
                item="",
                result=LogResult.FAILED,
                detail=detail,
            )
        elif total:
            self._log(
                level=LogLevel.INFO,
                watch="",
                action="retention",
                item="",
                result=LogResult.OK,
                detail=f"completed={completed} failed={failed} staging={staging}",
            )
        return {
            "completed": completed,
            "failed": failed,
            "staging": staging,
            "total": total,
            "errors": errors,
        }
