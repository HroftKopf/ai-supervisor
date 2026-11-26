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
