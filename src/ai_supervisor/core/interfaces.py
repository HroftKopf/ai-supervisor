from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(slots=True, frozen=True)
class Button:
    text: str
    callback_data: str


@dataclass(slots=True)
class Attachment:
    filename: str
    mime_type: str
    data: bytes
    is_image: bool = False

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(slots=True)
class IncomingMessage:
    chat_id: int
    user_id: int
    text: str
    telegram_message_id: int | None = None
    reply_to_message_id: int | None = None
    attachments: list[Attachment] = field(default_factory=list)


class Notifier(Protocol):
    async def send_text(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[Button]] | None = None,
    ) -> int | None: ...

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str = "",
    ) -> None: ...

    async def edit_buttons(
        self,
        chat_id: int,
        message_id: int,
        buttons: list[list[Button]] | None,
    ) -> None: ...
