from __future__ import annotations

import logging
from typing import Any

from organizer import runtime


def test_runtime_defaults_to_localhost(monkeypatch: Any) -> None:
    monkeypatch.delenv("ORGANIZER_HOST", raising=False)
    monkeypatch.delenv("ORGANIZER_PORT", raising=False)

    settings = runtime.RuntimeSettings.from_environment()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.is_loopback


def test_non_loopback_bind_logs_prominent_warning(caplog: Any) -> None:
    settings = runtime.RuntimeSettings(host="0.0.0.0")

    with caplog.at_level(logging.WARNING):
        runtime.log_startup_diagnostics(settings, logging.getLogger("test-organizer"))

    assert "unauthenticated UI" in caplog.text
    assert "non-loopback" in caplog.text


def test_missing_rar_tooling_is_reported() -> None:
    assert runtime.check_rar_tooling(lambda _name: None) == "RAR extraction tooling unavailable: install unrar or unar"


def test_available_rar_tooling_is_accepted() -> None:
    assert runtime.check_rar_tooling(lambda name: "/usr/bin/unrar" if name == "unrar" else None) is None
