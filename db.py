"""
Database layer for chat persistence.
Supports SQLite (default) and PostgreSQL via DATABASE_URL.
"""

import base64
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    BigInteger,
    Integer,
    String,
    create_engine,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DATABASE_URL, DEFAULT_INTERVAL_HOURS

logger = logging.getLogger(__name__)

Base = declarative_base()


CUSTOMER_BOT_TOKEN_SECRET = (os.getenv("CUSTOMER_BOT_TOKEN_SECRET") or "change-me").encode("utf-8")


def _encrypt_token(token: str) -> str:
    """Encrypt token with a simple XOR+base64 scheme."""
    raw = token.encode("utf-8")
    encrypted = bytes([b ^ CUSTOMER_BOT_TOKEN_SECRET[i % len(CUSTOMER_BOT_TOKEN_SECRET)] for i, b in enumerate(raw)])
    return base64.urlsafe_b64encode(encrypted).decode("utf-8")


def _decrypt_token(token_encrypted: str) -> str:
    """Decrypt token encrypted by _encrypt_token."""
    raw = base64.urlsafe_b64decode(token_encrypted.encode("utf-8"))
    decrypted = bytes([b ^ CUSTOMER_BOT_TOKEN_SECRET[i % len(CUSTOMER_BOT_TOKEN_SECRET)] for i, b in enumerate(raw)])
    return decrypted.decode("utf-8")


def _make_async_url(url: str) -> str:
    """Convert sync DB URL to async URL for SQLAlchemy 2.0."""
    if url.startswith("sqlite"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


class Chat(Base):
    """Chat/group record for autopost scheduling."""

    __tablename__ = "chats"

    chat_id = Column(BigInteger, primary_key=True)
    bot_index = Column(BigInteger, default=0, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    next_send_at = Column(DateTime(timezone=True), nullable=True)
    interval_hours = Column(Float, default=DEFAULT_INTERVAL_HOURS, nullable=False)

    def __repr__(self) -> str:
        return f"<Chat {self.chat_id} enabled={self.enabled}>"


class CustomerBot(Base):
    """Per-user bot settings for custom autopost bots."""

    __tablename__ = "customer_bots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_telegram_user_id = Column(BigInteger, nullable=False, index=True)
    bot_token_encrypted = Column(String, nullable=False)
    bot_username = Column(String, nullable=False)
    message_text = Column(String, nullable=False)
    interval_hours = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class BotGroup(Base):
    """Group subscriptions for each customer bot."""

    __tablename__ = "bot_groups"

    customer_bot_id = Column(Integer, primary_key=True, nullable=False, index=True)
    group_chat_id = Column(BigInteger, primary_key=True, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    next_send_at = Column(DateTime(timezone=True), nullable=True)


# Ensure data directory exists for SQLite
def _ensure_data_dir():
    url = DATABASE_URL
    if "sqlite" in url:
        path = url.replace("sqlite:///", "").split("?")[0]
        Path(path).parent.mkdir(parents=True, exist_ok=True)


_async_url = _make_async_url(DATABASE_URL)
_ensure_data_dir()

engine = create_async_engine(
    _async_url,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in _async_url else {},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_session():
    """Async context manager for database sessions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create tables if they don't exist. Add bot_index column if missing (v2 multi-bot)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            if "sqlite" in DATABASE_URL:
                await conn.execute(text("ALTER TABLE chats ADD COLUMN bot_index INTEGER DEFAULT 0 NOT NULL"))
            else:
                await conn.execute(text("ALTER TABLE chats ADD COLUMN IF NOT EXISTS bot_index BIGINT DEFAULT 0 NOT NULL"))
        except Exception:
            pass
    logger.info("Database initialized")


async def add_customer_bot(
    owner_telegram_user_id: int,
    bot_token: str,
    bot_username: str,
    message_text: str,
    interval_hours: float,
) -> int:
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO customer_bots
                (owner_telegram_user_id, bot_token_encrypted, bot_username, message_text, interval_hours, is_active, created_at)
                VALUES
                (:owner_telegram_user_id, :bot_token_encrypted, :bot_username, :message_text, :interval_hours, :is_active, :created_at)
                """
            ),
            {
                "owner_telegram_user_id": owner_telegram_user_id,
                "bot_token_encrypted": _encrypt_token(bot_token),
                "bot_username": bot_username,
                "message_text": message_text,
                "interval_hours": interval_hours,
                "is_active": True,
                "created_at": now,
            },
        )
        inserted_id = result.lastrowid
        if inserted_id is None:
            row = await session.execute(text("SELECT max(id) FROM customer_bots"))
            inserted_id = row.scalar_one()
        return int(inserted_id)


async def get_customer_bots(owner_telegram_user_id: int) -> list[CustomerBot]:
    async with get_session() as session:
        result = await session.execute(
            text("SELECT * FROM customer_bots WHERE owner_telegram_user_id = :owner_telegram_user_id ORDER BY id ASC"),
            {"owner_telegram_user_id": owner_telegram_user_id},
        )
        rows = result.fetchall()
        cols = [c.key for c in CustomerBot.__table__.columns]
        return [CustomerBot(**dict(zip(cols, row))) for row in rows]


async def get_customer_bot(bot_id: int, owner_telegram_user_id: int) -> CustomerBot | None:
    async with get_session() as session:
        result = await session.execute(
            text(
                "SELECT * FROM customer_bots WHERE id = :bot_id AND owner_telegram_user_id = :owner_telegram_user_id"
            ),
            {"bot_id": bot_id, "owner_telegram_user_id": owner_telegram_user_id},
        )
        row = result.fetchone()
        if row is None:
            return None
        return CustomerBot(**dict(zip([c.key for c in CustomerBot.__table__.columns], row)))


async def set_customer_bot_active(bot_id: int, owner_telegram_user_id: int, is_active: bool) -> bool:
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                UPDATE customer_bots
                SET is_active = :is_active
                WHERE id = :bot_id AND owner_telegram_user_id = :owner_telegram_user_id
                """
            ),
            {
                "is_active": is_active,
                "bot_id": bot_id,
                "owner_telegram_user_id": owner_telegram_user_id,
            },
        )
        return result.rowcount > 0


async def delete_customer_bot(bot_id: int, owner_telegram_user_id: int) -> bool:
    async with get_session() as session:
        await session.execute(
            text("DELETE FROM bot_groups WHERE customer_bot_id = :bot_id"),
            {"bot_id": bot_id},
        )
        result = await session.execute(
            text("DELETE FROM customer_bots WHERE id = :bot_id AND owner_telegram_user_id = :owner_telegram_user_id"),
            {"bot_id": bot_id, "owner_telegram_user_id": owner_telegram_user_id},
        )
        return result.rowcount > 0


async def add_or_update_chat(chat_id: int, enabled: bool = True, bot_index: int = 0) -> Chat:
    now = datetime.now(timezone.utc)
    next_send = now + timedelta(hours=DEFAULT_INTERVAL_HOURS)

    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO chats (chat_id, bot_index, enabled, created_at, last_sent_at, next_send_at, interval_hours)
                VALUES (:chat_id, :bot_index, :enabled, :created_at, NULL, :next_send_at, :interval_hours)
                ON CONFLICT (chat_id) DO UPDATE SET
                    bot_index = :bot_index,
                    enabled = :enabled,
                    next_send_at = :next_send_at
                """
            ),
            {
                "chat_id": chat_id,
                "bot_index": bot_index,
                "enabled": enabled,
                "created_at": now,
                "next_send_at": next_send,
                "interval_hours": DEFAULT_INTERVAL_HOURS,
            },
        )
        result = await session.execute(text("SELECT * FROM chats WHERE chat_id = :chat_id"), {"chat_id": chat_id})
        row = result.fetchone()
        return Chat(**dict(zip([c.key for c in Chat.__table__.columns], row)))


async def get_enabled_chats() -> list[Chat]:
    async with get_session() as session:
        result = await session.execute(text("SELECT * FROM chats WHERE enabled = TRUE ORDER BY next_send_at ASC"))
        rows = result.fetchall()
        cols = [c.key for c in Chat.__table__.columns]
        return [Chat(**dict(zip(cols, row))) for row in rows]


async def get_chat(chat_id: int) -> Chat | None:
    async with get_session() as session:
        result = await session.execute(text("SELECT * FROM chats WHERE chat_id = :chat_id"), {"chat_id": chat_id})
        row = result.fetchone()
        if row is None:
            return None
        return Chat(**dict(zip([c.key for c in Chat.__table__.columns], row)))


async def set_enabled(chat_id: int, enabled: bool) -> bool:
    async with get_session() as session:
        result = await session.execute(
            text("UPDATE chats SET enabled = :enabled WHERE chat_id = :chat_id"),
            {"chat_id": chat_id, "enabled": enabled},
        )
        return result.rowcount > 0


async def mark_disabled(chat_id: int) -> None:
    async with get_session() as session:
        await session.execute(text("UPDATE chats SET enabled = 0 WHERE chat_id = :chat_id"), {"chat_id": chat_id})


async def update_after_send(chat_id: int, next_send_at: datetime) -> None:
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        await session.execute(
            text(
                """
                UPDATE chats
                SET last_sent_at = :now, next_send_at = :next_send_at
                WHERE chat_id = :chat_id
                """
            ),
            {"chat_id": chat_id, "now": now, "next_send_at": next_send_at},
        )


async def get_due_chats() -> list[Chat]:
    now = datetime.now(timezone.utc)
    async with get_session() as session:
        result = await session.execute(
            text(
                """
                SELECT * FROM chats
                WHERE enabled = TRUE AND (next_send_at IS NULL OR next_send_at <= :now)
                ORDER BY next_send_at ASC
                """
            ),
            {"now": now},
        )
        rows = result.fetchall()
        cols = [c.key for c in Chat.__table__.columns]
        return [Chat(**dict(zip(cols, row))) for row in rows]
