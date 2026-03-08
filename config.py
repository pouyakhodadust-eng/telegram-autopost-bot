"""
Configuration for the Telegram bot.
Uses environment variables with sensible defaults.
"""

import os

from dotenv import load_dotenv

load_dotenv()
from pathlib import Path

# Bot token(s): single token or two for v2 multi-bot (strip so .env newlines/quotes don't break)
BOT_TOKEN: str = (os.getenv("BOT_TOKEN") or "").strip().strip('"').strip("'")
BOT_TOKEN_2: str = (os.getenv("BOT_TOKEN_2") or "").strip().strip('"').strip("'")


def get_bot_tokens() -> list[str]:
    """Return list of non-empty bot tokens (1 or 2 bots)."""
    return [t for t in (BOT_TOKEN, BOT_TOKEN_2) if t]

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

➡️ ☎️ t.me/gangster888l

➡️ ☎️ tr.ee/TMEQRwT9H-

➡️ ☎️ tr.ee/KOEQO3khFm"""

# Default interval in hours
DEFAULT_INTERVAL_HOURS: float = 4.0

# Rate limiting: min seconds between sends across all groups
MIN_SEND_INTERVAL_SECONDS: float = float(os.getenv("MIN_SEND_INTERVAL_SECONDS", "2.0"))

# Log level
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
