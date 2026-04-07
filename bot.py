"""
Telegram bot: group join detection, autopost scheduling, admin commands.
"""

import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.filters.command import CommandObject
from aiogram.types import ChatMemberUpdated, Message
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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


class CreateBotFlow(StatesGroup):
    waiting_token = State()
    waiting_message = State()
    waiting_interval = State()
    waiting_confirmation = State()


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


async def _validate_customer_bot_token(token: str) -> tuple[bool, str | None, str | None]:
    """Validate customer bot token via getMe call."""
    token = (token or "").strip()
    if not token:
        return False, None, "Token cannot be empty."

    validation_bot = None
    try:
        validation_bot = Bot(token=token)
        me = await validation_bot.get_me()
        username = me.username or ""
        return True, username, None
    except TokenValidationError:
        return False, None, "Invalid token format. Please paste a valid bot token from @BotFather."
    except Exception:
        return False, None, "Token validation failed via getMe. Make sure the token is correct and try again."
    finally:
        if validation_bot is not None:
            await validation_bot.session.close()


def _private_chat_only(message: Message) -> bool:
    return message.chat.type == "private"


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

    @dp.message(Command("create_bot"))
    async def cmd_create_bot(message: Message, state: FSMContext):
        if not _private_chat_only(message):
            await message.reply("Use /create_bot in a private chat with me.")
            return
        await state.clear()
        await state.set_state(CreateBotFlow.waiting_token)
        await message.reply("Step 1/5: Send your bot token.")

    @dp.message(CreateBotFlow.waiting_token)
    async def flow_waiting_token(message: Message, state: FSMContext):
        token = (message.text or "").strip()
        ok, username, err = await _validate_customer_bot_token(token)
        if not ok:
            await message.reply(f"❌ {err}\nPlease send the token again.")
            return

        await state.update_data(bot_token=token, bot_username=username)
        await state.set_state(CreateBotFlow.waiting_message)
        await message.reply("Step 3/5: Send the message text this bot should post.")

    @dp.message(CreateBotFlow.waiting_message)
    async def flow_waiting_message(message: Message, state: FSMContext):
        text = (message.text or "").strip()
        if not text:
            await message.reply("Message text cannot be empty. Send the message text.")
            return

        await state.update_data(message_text=text)
        await state.set_state(CreateBotFlow.waiting_interval)
        await message.reply("Step 4/5: Send interval in hours (float > 0), for example: 4 or 1.5")

    @dp.message(CreateBotFlow.waiting_interval)
    async def flow_waiting_interval(message: Message, state: FSMContext):
        value = (message.text or "").strip().replace(",", ".")
        try:
            interval = float(value)
            if interval <= 0:
                raise ValueError
        except ValueError:
            await message.reply("Interval must be a positive number (float > 0). Try again.")
            return

        await state.update_data(interval_hours=interval)
        data = await state.get_data()
        summary = (
            "Step 5/5: Confirm new bot configuration:\n"
            f"• Username: @{data['bot_username']}\n"
            f"• Message: {data['message_text']}\n"
            f"• Interval (hours): {interval}\n\n"
            "Reply with `yes` to save or `no` to cancel."
        )
        await state.set_state(CreateBotFlow.waiting_confirmation)
        await message.reply(summary, parse_mode=None)

    @dp.message(CreateBotFlow.waiting_confirmation)
    async def flow_waiting_confirmation(message: Message, state: FSMContext):
        reply = (message.text or "").strip().lower()
        if reply not in {"yes", "no"}:
            await message.reply("Please reply with `yes` or `no`.", parse_mode=None)
            return

        if reply == "no":
            await state.clear()
            await message.reply("Creation canceled.")
            return

        data = await state.get_data()
        owner_id = message.from_user.id
        created_id = await db.add_customer_bot(
            owner_telegram_user_id=owner_id,
            bot_token=data["bot_token"],
            bot_username=data["bot_username"],
            message_text=data["message_text"],
            interval_hours=float(data["interval_hours"]),
        )
        await state.clear()
        await message.reply(f"✅ Bot saved with id={created_id}.")

    @dp.message(Command("my_bots"))
    async def cmd_my_bots(message: Message):
        if not _private_chat_only(message):
            await message.reply("Use /my_bots in a private chat with me.")
            return
        bots = await db.get_customer_bots(message.from_user.id)
        if not bots:
            await message.reply("You don't have bots yet. Use /create_bot.")
            return

        rows = []
        for b in bots:
            state = "active" if b.is_active else "paused"
            rows.append(f"#{b.id} @{b.bot_username} | every {b.interval_hours}h | {state}")
        await message.reply("Your bots:\n" + "\n".join(rows))

    @dp.message(Command("pause_bot"))
    async def cmd_pause_bot(message: Message, command: CommandObject):
        if not _private_chat_only(message):
            await message.reply("Use /pause_bot in a private chat with me.")
            return
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.reply("Usage: /pause_bot <id>")
            return
        ok = await db.set_customer_bot_active(int(arg), message.from_user.id, False)
        await message.reply("Paused." if ok else "Bot not found.")

    @dp.message(Command("resume_bot"))
    async def cmd_resume_bot(message: Message, command: CommandObject):
        if not _private_chat_only(message):
            await message.reply("Use /resume_bot in a private chat with me.")
            return
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.reply("Usage: /resume_bot <id>")
            return
        ok = await db.set_customer_bot_active(int(arg), message.from_user.id, True)
        await message.reply("Resumed." if ok else "Bot not found.")

    @dp.message(Command("delete_bot"))
    async def cmd_delete_bot(message: Message, command: CommandObject):
        if not _private_chat_only(message):
            await message.reply("Use /delete_bot in a private chat with me.")
            return
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.reply("Usage: /delete_bot <id>")
            return
        ok = await db.delete_customer_bot(int(arg), message.from_user.id)
        await message.reply("Deleted." if ok else "Bot not found.")

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
        b._bot_index = i
        bots.append(b)

    await db.init_db()
    set_bots(bots)

    dispatchers = [Dispatcher() for _ in bots]
    for d in dispatchers:
        register_handlers(d)

    await send_to_all_enabled_chats()

    scheduler_task = asyncio.create_task(run_scheduler())

    logger.info("Starting %s bot(s)...", len(bots))
    try:
        await asyncio.gather(*[dp.start_polling(bot) for dp, bot in zip(dispatchers, bots)])
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
