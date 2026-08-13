from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from organizer.item_processor import BoundaryPolicy

_WATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(ValueError):
    """Raised when organizer.yaml cannot define a safe runtime configuration."""


class WatchFolderConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    watch_id: str
    watch_root: Path
    rules_path: Path
    boundary_policy: BoundaryPolicy = BoundaryPolicy()
    enabled: bool = True
    scan_interval: int | None = None


class OrganizerConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    config_path: Path
    config_root: Path
    database_path: Path
    log_path: Path
    scan_interval: int
    stability_interval: int
    log_level: str
    retention_days: int
    retention_interval: int
    data_roots: tuple[Path, ...]
    quarantine_root: Path
    watches: tuple[WatchFolderConfig, ...]

    def watch(self, watch_id: str) -> WatchFolderConfig:
        for watch in self.watches:
            if watch.watch_id == watch_id:
                return watch
        raise KeyError(watch_id)


def validate_watch_id(watch_id: str, existing_ids: list[str]) -> None:
    if not _WATCH_ID_PATTERN.match(watch_id):
        raise ConfigError(
            f"invalid watch id: {watch_id!r} (only A-Za-z0-9 _ - allowed)"
        )
    if any(
        watch_id.lower() == existing.lower() for existing in existing_ids
    ):
        raise ConfigError(f"duplicate watch id: {watch_id}")


def rebuild_boundary_policy(watches: list[WatchFolderConfig]) -> None:
    """Rebuild the shared BoundaryPolicy across the given watches in place."""
    if not watches:
        return
    policy = replace(
        watches[0].boundary_policy,
        watch_roots=tuple(watch.watch_root for watch in watches),
        watch_ids=tuple(watch.watch_id for watch in watches),
    )
    watches[:] = [
        watch.model_copy(update={"boundary_policy": policy}) for watch in watches
    ]


def validate_watch_root(
    root: Path,
    config_root: Path,
    data_roots: list[Path] | tuple[Path, ...],
    existing_roots: list[Path] | tuple[Path, ...],
    watch_id: str | None = None,
) -> None:
    context = f"watch {watch_id}" if watch_id is not None else "watch"
    if _within(root, config_root):
        raise ConfigError(f"{context} is within the config volume")
    if not any(_within(root, data_root) for data_root in data_roots):
        raise ConfigError(f"{context} is outside data volumes")
    for other in existing_roots:
        if _within(root, other) or _within(other, root):
            raise ConfigError(f"{context} roots overlap")


def load_config(path: Path = Path("/config/organizer.yaml")) -> OrganizerConfig:
    config_path = path.expanduser().resolve()
    try:
        document = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot load config: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError("config must be a mapping")

    config_root = _path(document, "config_root", config_path.parent, config_path.parent)
    database_path = _path(document, "database", config_root / "organizer.db", config_path.parent)
    log_path = _path(document, "log_path", config_root / "logs/organizer.log", config_path.parent)
    data_roots = _paths(document.get("data_roots", ["/data"]), "data_roots", config_path.parent)
    quarantine_root = _path(document, "quarantine_root", config_root / "quarantine", config_path.parent)
    scan_interval = _positive_int(document.get("scan_interval", 300), "scan_interval")
    stability_interval = _non_negative_int(document.get("stability_interval", 5), "stability_interval")
    retention_days = _positive_int(document.get("retention_days", 7), "retention_days")
    retention_interval = _positive_int(document.get("retention_interval", 3600), "retention_interval")
    log_level = document.get("log_level", "INFO")
    if not isinstance(log_level, str) or log_level.upper() not in {"DEBUG", "INFO", "WARN", "ERROR"}:
        raise ConfigError("log_level must be DEBUG, INFO, WARN, or ERROR")
    raw_watches = document.get("watches")
    if not isinstance(raw_watches, list) or not raw_watches:
        raise ConfigError("watches must be a non-empty list")

    if document.get("quarantine_root") is not None and (_within(quarantine_root, config_root) or not any(_within(quarantine_root, data_root) for data_root in data_roots)):
        raise ConfigError("quarantine root must be within a data volume and outside the config volume")

    base_policy = BoundaryPolicy(
        data_roots=tuple(data_roots),
        config_root=config_root,
        allowed_destinations=tuple(data_roots),
        quarantine_root=quarantine_root,
    )
    watches: list[WatchFolderConfig] = []
    for raw_watch in raw_watches:
        if not isinstance(raw_watch, dict):
            raise ConfigError("watch must be a mapping")
        watch_id = raw_watch.get("id")
        if not isinstance(watch_id, str) or not watch_id:
            raise ConfigError("watch id is required")
        validate_watch_id(watch_id, [watch.watch_id for watch in watches])
        enabled = raw_watch.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"watch {watch_id} enabled must be a boolean")
        raw_scan_interval = raw_watch.get("scan_interval")
        if raw_scan_interval is None:
            watch_scan_interval = None
        else:
            watch_scan_interval = _positive_int(
                raw_scan_interval, f"watch {watch_id} scan_interval"
            )
        root = _required_path(raw_watch, "root", f"watch {watch_id}", config_path.parent)
        rules_value = raw_watch.get("rules", "rules.yaml")
        if not isinstance(rules_value, str) or not rules_value:
            raise ConfigError(f"watch {watch_id} rules path is required")
        rules_path = Path(rules_value)
        if not rules_path.is_absolute():
            rules_path = config_path.parent / rules_path
        validate_watch_root(
            root,
            config_root,
            data_roots,
            [watch.watch_root for watch in watches],
            watch_id,
        )
        watches.append(
            WatchFolderConfig(
                watch_id=watch_id,
                watch_root=root,
                rules_path=rules_path.resolve(),
                boundary_policy=base_policy,
                enabled=enabled,
                scan_interval=watch_scan_interval,
            )
        )
    rebuild_boundary_policy(watches)
    return OrganizerConfig(
        config_path=config_path,
        config_root=config_root,
        database_path=database_path,
        log_path=log_path,
        scan_interval=scan_interval,
        stability_interval=stability_interval,
        log_level=log_level.upper(),
        retention_days=retention_days,
        retention_interval=retention_interval,
        data_roots=tuple(data_roots),
        quarantine_root=quarantine_root,
        watches=tuple(watches),
    )


def _path(document: dict[str, Any], key: str, default: Path, base: Path) -> Path:
    value = document.get(key, str(default))
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a path")
    result = Path(value)
    return result.resolve() if result.is_absolute() else (base / result).resolve()


def _required_path(document: dict[str, Any], key: str, context: str, base: Path) -> Path:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{context} {key} is required")
    result = Path(value)
    return (result if result.is_absolute() else base / result).resolve()


def _paths(value: object, key: str, base: Path) -> list[Path]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{key} must be a non-empty list of paths")
    return [(Path(item) if Path(item).is_absolute() else base / Path(item)).resolve() for item in value]


def _positive_int(value: object, key: str) -> int:
    return _int_with_min(value, key, 1, "positive")


def _non_negative_int(value: object, key: str) -> int:
    return _int_with_min(value, key, 0, "non-negative")


def _int_with_min(value: object, key: str, minimum: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{key} must be a {label} integer")
    return value


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
