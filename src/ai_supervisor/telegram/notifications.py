from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from ai_supervisor.config import Settings
from ai_supervisor.core.interfaces import Button
from ai_supervisor.core.redaction import SecretRedactor

logger = logging.getLogger(__name__)


class BotNotifier:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self.redactor = SecretRedactor.from_settings(settings)

    async def send_text(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[Button]] | None = None,
        *,
        parse_mode: str | None = None,
    ) -> int | None:
        text = self.redactor.redact(text or "(пусто)")
        limit = self.settings.telegram.max_message_chars
        chunks = _split_message(text, limit)
        first_message_id: int | None = None
        for index, chunk in enumerate(chunks):
            markup = self._markup(buttons) if index == 0 and buttons else None
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=markup,
                disable_web_page_preview=True,
                parse_mode=parse_mode,
            )
            if first_message_id is None:
                first_message_id = message.message_id
        return first_message_id

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> None:
        document = FSInputFile(str(path), filename=path.name)
        await self.bot.send_document(
            chat_id=chat_id, document=document, caption=self.redactor.redact(caption[:1024])
        )

    async def edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: list[list[Button]] | None = None,
        *,
        parse_mode: str | None = None,
    ) -> bool:
        text = self.redactor.redact(text or "(пусто)")
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text[: self.settings.telegram.max_message_chars],
                reply_markup=self._markup(buttons) if buttons else None,
                disable_web_page_preview=True,
                parse_mode=parse_mode,
            )
            return True
        except TelegramRetryAfter as exc:
            logger.warning("Telegram edit rate limited; retry after %s", exc.retry_after)
            await asyncio.sleep(min(float(exc.retry_after), 5.0))
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text[: self.settings.telegram.max_message_chars],
                    reply_markup=self._markup(buttons) if buttons else None,
                    disable_web_page_preview=True,
                    parse_mode=parse_mode,
                )
                return True
            except (TelegramRetryAfter, TelegramBadRequest):
                return False
        except TelegramBadRequest as exc:
            logger.debug("edit_text failed: %s", exc)
            return False

    async def edit_buttons(
        self,
        chat_id: int,
        message_id: int,
        buttons: list[list[Button]] | None,
    ) -> None:
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=self._markup(buttons) if buttons else None,
            )
        except TelegramBadRequest as exc:
            logger.debug("edit_buttons ignored: %s", exc)

    @staticmethod
    def _markup(buttons: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
        if not buttons:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button.text,
                        callback_data=button.callback_data or None,
                        url=button.url,
                    )
                    for button in row
                ]
                for row in buttons
            ]
        )


def _split_message(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        candidate = remaining[:limit]
        split = candidate.rfind("\n")
        if split < limit // 2:
            split = candidate.rfind(" ")
        if split < limit // 2:
            split = limit
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    return chunks or [""]
