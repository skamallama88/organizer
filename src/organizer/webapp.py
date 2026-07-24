from pathlib import Path

from organizer.item_processor import ItemProcessor
from organizer.operational_health import OperationalHealth
from organizer.structured_log import MemoryLogSink, StdoutLogSink, StructuredLogger
from organizer.web import create_app

db_path = Path("/config/organizer.db")
log_sink = MemoryLogSink(limit=1000)
logger = StructuredLogger(sinks=[StdoutLogSink(), log_sink])
health_checker = OperationalHealth()

app = create_app(
    ItemProcessor(attempts_path=db_path, logger=logger, health_checker=health_checker),
    log_sink=log_sink,
    health_checker=health_checker,
    watch_folders=[],
    db_path=db_path,
)
