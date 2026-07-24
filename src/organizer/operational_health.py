from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WatchFolderHealth:
    watch_id: str
    accessible: bool
    detail: str = ""


@dataclass(frozen=True)
class PersistenceHealth:
    tracking_db_writable: bool
    detail: str = ""


@dataclass(frozen=True)
class OverallHealth:
    all_healthy: bool
    watch_folder_healths: tuple[WatchFolderHealth, ...]
    persistence_health: PersistenceHealth


class OperationalHealth:
    def check_watch_folder(self, watch_id: str, watch_root: Path) -> WatchFolderHealth:
        if not watch_root.exists():
            return WatchFolderHealth(
                watch_id=watch_id,
                accessible=False,
                detail=f"watch root does not exist: {watch_root}",
            )
        if not watch_root.is_dir():
            return WatchFolderHealth(
                watch_id=watch_id,
                accessible=False,
                detail=f"watch root is not a directory: {watch_root}",
            )
        if not os.access(watch_root, os.R_OK):
            return WatchFolderHealth(
                watch_id=watch_id,
                accessible=False,
                detail=f"watch root is not readable: {watch_root}",
            )
        return WatchFolderHealth(watch_id=watch_id, accessible=True)

    def check_persistence(self, db_path: Path) -> PersistenceHealth:
        parent = db_path.parent
        if not parent.exists():
            return PersistenceHealth(
                tracking_db_writable=False,
                detail=f"database parent directory does not exist: {parent}",
            )
        if not os.access(parent, os.W_OK):
            return PersistenceHealth(
                tracking_db_writable=False,
                detail=f"database parent directory is not writable: {parent}",
            )
        try:
            with sqlite3.connect(db_path, timeout=1) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS _health_check (id INTEGER)")
                conn.execute("INSERT INTO _health_check VALUES (1)")
                conn.execute("DELETE FROM _health_check")
        except sqlite3.Error as error:
            return PersistenceHealth(
                tracking_db_writable=False,
                detail=f"database error: {error}",
            )
        return PersistenceHealth(tracking_db_writable=True)

    def check_all(
        self,
        watch_folders: list[tuple[str, Path]],
        db_path: Path,
    ) -> OverallHealth:
        watch_healths = tuple(
            self.check_watch_folder(watch_id, root)
            for watch_id, root in watch_folders
        )
        persistence = self.check_persistence(db_path)
        all_healthy = (
            all(h.accessible for h in watch_healths)
            and persistence.tracking_db_writable
        )
        return OverallHealth(
            all_healthy=all_healthy,
            watch_folder_healths=watch_healths,
            persistence_health=persistence,
        )
