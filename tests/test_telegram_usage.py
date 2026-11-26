from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_supervisor.telegram.handlers import Handlers


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def send_text(self, chat_id: int, text: str, buttons=None, *, parse_mode=None):
        self.calls.append((text, parse_mode))
        return 1


@pytest.mark.asyncio
async def test_usage_command_uses_html_parse_mode(settings) -> None:
    notifier = RecordingNotifier()
    orchestrator = SimpleNamespace(
        usage_text=AsyncMock(return_value="<b>Расход API</b>\n\nсегодня: 10"),
        settings=settings,
    )
    handlers = Handlers(
        settings=settings,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        projects=SimpleNamespace(),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
    )
    message = SimpleNamespace(chat=SimpleNamespace(id=424242), text="/usage today")
    await handlers.cmd_usage(message)
    assert notifier.calls == [("<b>Расход API</b>\n\nсегодня: 10", "HTML")]


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=424242)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=424242))
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kwargs):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_usage_callback_and_menu_use_html(settings) -> None:
    notifier = RecordingNotifier()
    orchestrator = SimpleNamespace(
        usage_text=AsyncMock(return_value="<b>Расход API</b>\n\nитог"),
        tasks_text=AsyncMock(return_value="tasks"),
        status_text=AsyncMock(return_value="status"),
        conversations=SimpleNamespace(set_ui_mode=AsyncMock()),
        progress=SimpleNamespace(update=AsyncMock()),
        redactor=SimpleNamespace(redact=lambda value: value),
        settings=settings,
    )
    handlers = Handlers(
        settings=settings,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        projects=SimpleNamespace(),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
    )
    await handlers.on_callback(FakeCallback("use:5"))
    await handlers.on_callback(FakeCallback("menu:usage"))
    assert [mode for _text, mode in notifier.calls] == ["HTML", "HTML"]
