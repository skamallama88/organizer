import logging

from organizer.item_processor import ItemProcessor
from organizer.config import load_config
from organizer.operational_health import OperationalHealth
from organizer.runtime import RuntimeSettings, log_startup_diagnostics
from organizer.structured_log import build_logger
from organizer.web import create_app

config = load_config()
db_path = config.database_path
logger, log_sink = build_logger(log_path=config.log_path, retention_days=config.retention_days)
health_checker = OperationalHealth()

app = create_app(
    ItemProcessor(attempts_path=db_path, logger=logger, health_checker=health_checker),
    log_sink=log_sink,
    health_checker=health_checker,
    watch_folders=config.watches,
    db_path=db_path,
    config_path=config.config_path,
)

settings = RuntimeSettings.from_environment()
log_startup_diagnostics(settings, logging.getLogger("organizer"))


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
