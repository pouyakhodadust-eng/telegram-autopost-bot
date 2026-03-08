"""
Telegram bot: group join detection, autopost scheduling, admin commands.
"""

import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message
from aiogram.enums import ChatMemberStatus
from aiogram.utils.token import TokenValidationError

from config import (
    CONTACT_MESSAGE,
    DEFAULT_INTERVAL_HOURS,
    get_bot_tokens,
    LOG_LEVEL,
)
import db
from scheduler import run_scheduler, send_to_all_enabled_chats, set_bots

# Structured logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _bot_index(bot_instance: Bot) -> int:
    """Return the bot index for scheduler/DB (0 or 1)."""
    return getattr(bot_instance, "_bot_index", 0)


def _is_group(chat_type: str) -> bool:
    return chat_type in ("group", "supergroup")


def _bot_was_added(update: ChatMemberUpdated) -> bool:
    """True if the bot changed from left/kicked to member/admin."""
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    was_out = old in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, "left", "kicked")
    is_in = new in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        "member",
        "administrator",
    )
    return was_out and is_in


def _bot_was_removed(update: ChatMemberUpdated) -> bool:
    """True if the bot changed to left or kicked."""
    new = update.new_chat_member.status
    return new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED, "left", "kicked")


async def _is_chat_admin(bot_instance: Bot, chat_id: int, user_id: int) -> bool:
    """Check if user is admin or creator in the chat."""
    try:
        member = await bot_instance.get_chat_member(chat_id, user_id)
        status = getattr(member, "status", None) or str(member.status)
        return status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            "administrator",
            "creator",
        )
    except Exception:
        return False


# --- Handlers (registered per dispatcher for multi-bot) ---


def register_handlers(dp: Dispatcher) -> None:
    """Register all handlers on the given dispatcher."""

    @dp.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
    async def on_chat_member_updated(update: ChatMemberUpdated):
        """Handle bot being added/removed from groups."""
        chat_id = update.chat.id
        chat_type = update.chat.type

        if not _is_group(chat_type):
            return

        bot_idx = _bot_index(update.bot)
        if _bot_was_added(update):
            logger.info("Bot %s added to group %s", bot_idx, chat_id)
            try:
                await db.add_or_update_chat(chat_id, enabled=True, bot_index=bot_idx)
                await update.bot.send_message(
                    chat_id=chat_id,
                    text=CONTACT_MESSAGE,
                    disable_web_page_preview=True,
                )
                logger.info("Immediate message sent to %s", chat_id)
            except Exception as e:
                logger.exception("Error on join for %s: %s", chat_id, e)
                try:
                    await db.mark_disabled(chat_id)
                except Exception:
                    pass

        elif _bot_was_removed(update):
            logger.info("Bot %s removed from group %s", bot_idx, chat_id)
            try:
                await db.mark_disabled(chat_id)
            except Exception as e:
                logger.warning("Failed to mark disabled for %s: %s", chat_id, e)

    @dp.message(Command("enable_autopost"))
    async def cmd_enable_autopost(message: Message):
        """Enable daily autopost (admin only)."""
        if message.chat.type not in ("group", "supergroup"):
            await message.reply("This command works only in groups.")
            return
        if not await _is_chat_admin(message.bot, message.chat.id, message.from_user.id):
            await message.reply("Only group admins can use this command.")
            return

        chat_id = message.chat.id
        bot_idx = _bot_index(message.bot)
        record = await db.get_chat(chat_id)
        if record is None:
            await db.add_or_update_chat(chat_id, enabled=True, bot_index=bot_idx)
            await message.reply(f"Autopost enabled. Messages will be sent every {int(DEFAULT_INTERVAL_HOURS)} hours.")
        else:
            await db.set_enabled(chat_id, True)
            await message.reply("Autopost enabled.")

    @dp.message(Command("disable_autopost"))
    async def cmd_disable_autopost(message: Message):
        """Disable daily autopost (admin only)."""
        if message.chat.type not in ("group", "supergroup"):
            await message.reply("This command works only in groups.")
            return
        if not await _is_chat_admin(message.bot, message.chat.id, message.from_user.id):
            await message.reply("Only group admins can use this command.")
            return

        chat_id = message.chat.id
        await db.set_enabled(chat_id, False)
        await message.reply("Autopost disabled.")

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        """Show autopost status (admin only)."""
        if message.chat.type not in ("group", "supergroup"):
            await message.reply("This command works only in groups.")
            return
        if not await _is_chat_admin(message.bot, message.chat.id, message.from_user.id):
            await message.reply("Only group admins can use this command.")
            return

        chat_id = message.chat.id
        record = await db.get_chat(chat_id)
        if record is None:
            await message.reply("This chat is not registered. Add the bot first or use /enable_autopost.")
            return

        last = record.last_sent_at.strftime("%Y-%m-%d %H:%M UTC") if record.last_sent_at else "Never"
        next_ = record.next_send_at.strftime("%Y-%m-%d %H:%M UTC") if record.next_send_at else "Not scheduled"
        status = "Enabled" if record.enabled else "Disabled"
        text = f"Autopost: {status}\nLast sent: {last}\nNext send: {next_}"
        await message.reply(text)


async def main():
    tokens = get_bot_tokens()
    if not tokens:
        logger.error("No BOT_TOKEN set. Set BOT_TOKEN (and optionally BOT_TOKEN_2) in .env")
        sys.exit(1)

    bots = []
    for i, token in enumerate(tokens):
        try:
            b = Bot(token=token)
        except TokenValidationError:
            name = "BOT_TOKEN" if i == 0 else "BOT_TOKEN_2"
            logger.error(
                "%s (bot index %s) is invalid. Check .env: no quotes, no extra spaces, exact token from @BotFather.",
                name,
                i,
            )
            sys.exit(1)
        b._bot_index = i  # for scheduler and handlers
        bots.append(b)

    await db.init_db()
    set_bots(bots)

    # One dispatcher per bot, same handlers
    dispatchers = [Dispatcher() for _ in bots]
    for d in dispatchers:
        register_handlers(d)

    # Send once to all enabled groups (startup broadcast)
    await send_to_all_enabled_chats()

    # Start scheduler in background
    scheduler_task = asyncio.create_task(run_scheduler())

    logger.info("Starting %s bot(s)...", len(bots))
    try:
        await asyncio.gather(
            *[dp.start_polling(bot) for dp, bot in zip(dispatchers, bots)]
        )
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        for b in bots:
            await b.session.close()


if __name__ == "__main__":
    asyncio.run(main())
