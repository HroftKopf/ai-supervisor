from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, Update

from ai_supervisor.config import Settings
from ai_supervisor.memory.database import Database, iso_now

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DedupeMiddleware(BaseMiddleware):
    """Protect against Telegram re-delivering the same update after a restart."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        update_id: int | None = None
        if isinstance(event, Update):
            update_id = event.update_id
        else:
            update_id = getattr(event, "update_id", None)
        if update_id is None:
            return await handler(event, data)
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO processed_updates(update_id, processed_at) VALUES (?, ?)",
            (update_id, iso_now()),
        )
        inserted = cursor.rowcount > 0
        await cursor.close()
        if not inserted:
            logger.info("duplicate Telegram update ignored: %s", update_id)
            return None
        return await handler(event, data)


class OwnerMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            callback_message = getattr(event, "message", None)
            chat = getattr(callback_message, "chat", None)
        if user is None or getattr(user, "is_bot", False):
            await self._deny(event)
            return None
        if self.settings.owner_id != user.id:
            logger.warning("unauthorized Telegram user denied: id=%s", user.id)
            await self._deny(event)
            return None
        if self.settings.telegram.private_chat_only:
            if chat is None or chat.type != "private":
                await self._deny(event, "Бот работает только в личном чате владельца.")
                return None
        # Private chat id must equal the numeric owner id in Telegram.
        if (
            chat is not None
            and self.settings.telegram.private_chat_only
            and chat.id != self.settings.owner_id
        ):
            await self._deny(event)
            return None
        return await handler(event, data)

    @staticmethod
    async def _deny(event: TelegramObject, message: str = "Доступ запрещён.") -> None:
        try:
            answer = getattr(event, "answer", None)
            if answer is not None:
                if isinstance(event, CallbackQuery):
                    await answer(message, show_alert=True)
                else:
                    await answer(message)
        except Exception:
            logger.exception("failed to send authorization denial")


class AuthMiddleware(OwnerMiddleware):
    """Backward-compatible alias used in wiring."""
