"""
Scheduler for periodic message posting.
Uses a custom async loop that polls the database - no external job store needed.
All state is in our DB; survives restarts; no duplicate jobs.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from config import CONTACT_MESSAGE, DEFAULT_INTERVAL_HOURS, MIN_SEND_INTERVAL_SECONDS
import db

logger = logging.getLogger(__name__)

# Check interval: how often we look for due chats (seconds)
POLL_INTERVAL = 60

# Bot instance will be set by main
_bot = None


def set_bot(bot):
    """Set the bot instance for sending messages."""
    global _bot
    _bot = bot


async def _send_message(chat_id: int) -> bool:
    """
    Send the contact message to a chat.
    Returns True on success, False on failure (e.g. bot removed, no permission).
    """
    if _bot is None:
        logger.error("Bot not set in scheduler")
        return False
    try:
        await _bot.send_message(
            chat_id=chat_id,
            text=CONTACT_MESSAGE,
            disable_web_page_preview=True,  # No link previews
        )
        return True
    except Exception as e:
        logger.warning("Failed to send to chat %s: %s", chat_id, e)
        return False


async def _process_due_chats():
    """
    Process all chats that are due for sending.
    Sends one message per chat, then schedules next 24h later.
    Rate-limits between sends.
    """
    due = await db.get_due_chats()
    if not due:
        return
    logger.info("Processing %d due chat(s)", len(due))
    now = datetime.now(timezone.utc)
    next_send = now + timedelta(hours=DEFAULT_INTERVAL_HOURS)
    for chat in due:
        success = await _send_message(chat.chat_id)
        if success:
            await db.update_after_send(chat.chat_id, next_send)
            logger.info("Sent to chat %s, next at %s", chat.chat_id, next_send)
        else:
            # Bot may have been removed or lost permission
            await db.mark_disabled(chat.chat_id)
            logger.info("Disabled chat %s after send failure", chat.chat_id)
        # Rate limit between groups
        await asyncio.sleep(MIN_SEND_INTERVAL_SECONDS)


async def send_to_all_enabled_chats():
    """
    On startup: send the message once to every enabled group in the DB.
    Use after set_bot() so the bot is available. Rate-limited.
    """
    chats = await db.get_enabled_chats()
    if not chats:
        logger.info("No enabled chats to send to on startup")
        return
    logger.info("Startup: sending message to %d enabled chat(s)", len(chats))
    now = datetime.now(timezone.utc)
    next_send = now + timedelta(hours=DEFAULT_INTERVAL_HOURS)
    for chat in chats:
        success = await _send_message(chat.chat_id)
        if success:
            await db.update_after_send(chat.chat_id, next_send)
            logger.info("Startup message sent to chat %s", chat.chat_id)
        else:
            await db.mark_disabled(chat.chat_id)
            logger.info("Disabled chat %s after startup send failure", chat.chat_id)
        await asyncio.sleep(MIN_SEND_INTERVAL_SECONDS)


async def run_scheduler():
    """
    Main scheduler loop.
    Polls DB for due chats at POLL_INTERVAL, processes them.
    Handles restarts: if bot was offline, sends one catch-up then next in 24h.
    """
    logger.info("Scheduler started, polling every %ds", POLL_INTERVAL)
    while True:
        try:
            await _process_due_chats()
        except Exception as e:
            logger.exception("Scheduler error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)
