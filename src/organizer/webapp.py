from pathlib import Path

from organizer.item_processor import ItemProcessor
from organizer.web import create_app

app = create_app(ItemProcessor(attempts_path=Path("/config/organizer.db")))
