from pathlib import Path
import logging

from organizer.item_processor import ItemProcessor
from organizer.operational_health import OperationalHealth
from organizer.runtime import RuntimeSettings, log_startup_diagnostics
from organizer.structured_log import MemoryLogSink, RotatingFileLogSink, StdoutLogSink, StructuredLogger
from organizer.web import create_app

db_path = Path("/config/organizer.db")
log_path = Path("/config/logs/organizer.log")
log_sink = MemoryLogSink(limit=1000)
logger = StructuredLogger(sinks=[StdoutLogSink(), RotatingFileLogSink(log_path), log_sink])
health_checker = OperationalHealth()

app = create_app(
    ItemProcessor(attempts_path=db_path, logger=logger, health_checker=health_checker),
    log_sink=log_sink,
    health_checker=health_checker,
    watch_folders=[],
    db_path=db_path,
)

settings = RuntimeSettings.from_environment()
log_startup_diagnostics(settings, logging.getLogger("organizer"))


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
