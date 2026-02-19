"""
Configuration for the Telegram bot.
Uses environment variables with sensible defaults.
"""

import os

from dotenv import load_dotenv

load_dotenv()
from pathlib import Path

# Bot token (required)
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Database: SQLite by default, PostgreSQL via DATABASE_URL
# SQLite: sqlite:///path/to/bot.db
# PostgreSQL: postgresql://user:pass@host:5432/dbname
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).parent / 'data' / 'bot.db'}",
)

# Message to send (exact text, no variations)
CONTACT_MESSAGE: str = """MESSAGE US AT

CONTACT

➡️ ☎️ tr.ee/g8c3_3jgp2

➡️ ☎️ tr.ee/jwOQKG2fgU"""

# Default interval in hours
DEFAULT_INTERVAL_HOURS: float = 3.0

# Rate limiting: min seconds between sends across all groups
MIN_SEND_INTERVAL_SECONDS: float = float(os.getenv("MIN_SEND_INTERVAL_SECONDS", "2.0"))

# Log level
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
