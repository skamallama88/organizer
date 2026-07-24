from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import Callable

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
RAR_TOOL_NAMES = ("unrar", "unar")


@dataclass(frozen=True)
class RuntimeSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @classmethod
    def from_environment(cls) -> "RuntimeSettings":
        return cls(
            host=os.environ.get("ORGANIZER_HOST", DEFAULT_HOST),
            port=int(os.environ.get("ORGANIZER_PORT", str(DEFAULT_PORT))),
        )

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "::1", "localhost"}


def check_rar_tooling(which: Callable[[str], str | None] = shutil.which) -> str | None:
    if any(which(name) for name in RAR_TOOL_NAMES):
        return None
    return "RAR extraction tooling unavailable: install unrar or unar"


def log_startup_diagnostics(settings: RuntimeSettings, logger: logging.Logger) -> None:
    if not settings.is_loopback:
        logger.warning(
            "WARNING: Organizer unauthenticated UI is exposed on non-loopback host %s; "
            "use only behind a trusted reverse proxy or private network",
            settings.host,
        )
    rar_error = check_rar_tooling()
    if rar_error:
        logger.error(rar_error)
