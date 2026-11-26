from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_supervisor.telegram.handlers import Handlers


class Notifier:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def send_text(self, chat_id, text, buttons=None, *, parse_mode=None):
        self.calls.append((chat_id, text))
        return 1


def _handlers(settings, orchestrator, notifier):
    return Handlers(
        settings=settings,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        projects=SimpleNamespace(),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_explicit_chat_task_and_auto_modes(settings) -> None:
    notifier = Notifier()
    orchestrator = SimpleNamespace(
        chat_direct=AsyncMock(),
        create_task_from_text=AsyncMock(),
        handle_incoming=AsyncMock(),
    )
    handlers = _handlers(settings, orchestrator, notifier)
    message = SimpleNamespace(chat=SimpleNamespace(id=424242), from_user=SimpleNamespace(id=424242))
    await handlers.cmd_chat(SimpleNamespace(**vars(message), text="/chat explain"))
    await handlers.cmd_task(SimpleNamespace(**vars(message), text="/task do it"))
    await handlers.cmd_auto(SimpleNamespace(**vars(message), text="/auto"))
    orchestrator.chat_direct.assert_awaited_once()
    orchestrator.create_task_from_text.assert_awaited_once()
    assert notifier.calls[-1][1].startswith("Режим auto")
