from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_supervisor.telegram.auth import DedupeMiddleware, OwnerMiddleware


class FakeEvent:
    def __init__(
        self, user_id: int, chat_id: int, chat_type: str = "private", update_id: int | None = 1
    ) -> None:
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.update_id = update_id
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_owner_middleware_allows_only_numeric_owner(settings) -> None:
    middleware = OwnerMiddleware(settings)
    calls: list[str] = []

    async def handler(event, data):  # type: ignore[no-untyped-def]
        calls.append("called")
        return "ok"

    owner = FakeEvent(424242, 424242)
    assert await middleware(handler, owner, {}) == "ok"
    outsider = FakeEvent(999, 999)
    assert await middleware(handler, outsider, {}) is None
    assert outsider.answers
    assert calls == ["called"]


@pytest.mark.asyncio
async def test_group_chat_is_denied(settings) -> None:
    middleware = OwnerMiddleware(settings)

    async def handler(event, data):  # type: ignore[no-untyped-def]
        return "ok"

    event = FakeEvent(424242, -100123, chat_type="group")
    assert await middleware(handler, event, {}) is None
    assert event.answers


@pytest.mark.asyncio
async def test_duplicate_update_is_ignored(database, settings) -> None:
    middleware = DedupeMiddleware(database)
    calls: list[int] = []

    async def handler(event, data):  # type: ignore[no-untyped-def]
        calls.append(event.update_id)
        return "ok"

    event = FakeEvent(424242, 424242, update_id=777)
    assert await middleware(handler, event, {}) == "ok"
    assert await middleware(handler, event, {}) is None
    assert calls == [777]
