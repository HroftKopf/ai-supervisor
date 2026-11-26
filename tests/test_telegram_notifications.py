from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_supervisor.core.interfaces import Button
from ai_supervisor.telegram.notifications import BotNotifier


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_notifier_uses_html_buttons_and_redaction(settings) -> None:
    bot = FakeBot()
    notifier = BotNotifier(bot, settings)  # type: ignore[arg-type]
    await notifier.send_text(
        1,
        "<b>OK</b> " + settings.kimi_key,
        [[Button("Открыть", url="https://example.com"), Button("Info", "info:1")]],
        parse_mode="HTML",
    )
    sent = bot.sent[0]
    assert sent["parse_mode"] == "HTML"
    assert settings.kimi_key not in sent["text"]
    assert sent["reply_markup"].inline_keyboard[0][0].url == "https://example.com"
    assert sent["reply_markup"].inline_keyboard[0][1].callback_data == "info:1"
    assert await notifier.edit_text(1, 2, "<b>edit</b>", parse_mode="HTML") is True
    assert bot.edited[0]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_notifier_splits_long_messages_without_losing_text(settings) -> None:
    bot = FakeBot()
    settings.telegram.max_message_chars = 500
    notifier = BotNotifier(bot, settings)  # type: ignore[arg-type]
    text = ("paragraph " * 200).strip()
    await notifier.send_text(1, text)
    assert len(bot.sent) > 1
    assert all(len(item["text"]) <= 500 for item in bot.sent)
    assert "paragraph" in "".join(item["text"] for item in bot.sent)
