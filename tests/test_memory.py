from __future__ import annotations

import pytest

from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.tasks import TaskService
from ai_supervisor.models import TaskStatus, TransitionError


@pytest.mark.asyncio
async def test_messages_and_fts_survive_reconnect(database, settings, project) -> None:
    conversations = ConversationService(database, settings)
    conversation = await conversations.get_or_create_conversation(1, project.id)
    await conversations.add_message(conversation.id, "user", "уникальное слово celery-42")
    await database.close()
    await database.connect()
    conversations = ConversationService(database, settings)
    results = await conversations.search_messages("celery-42")
    assert len(results) == 1
    assert "celery-42" in results[0]["content"]
    recent = await conversations.recent_messages(conversation.id)
    assert recent[0].content == "уникальное слово celery-42"


@pytest.mark.asyncio
async def test_invalid_state_transition_is_rejected(database, project) -> None:
    # The TaskService only needs the DB; use a direct conversation row.
    from ai_supervisor.memory.database import iso_now

    await database.execute(
        "INSERT INTO conversations(chat_id, project_id, created_at, updated_at) VALUES (1, ?, ?, ?)",
        (project.id, iso_now(), iso_now()),
    )
    conversation_id = int(await database.scalar("SELECT id FROM conversations LIMIT 1"))
    tasks = TaskService(database)
    task = await tasks.create(project.id, conversation_id, "state test", max_rework=1)
    with pytest.raises(TransitionError):
        await tasks.transition(task.id, TaskStatus.COMPLETED)
    await tasks.transition(task.id, TaskStatus.CANCELLED)
    with pytest.raises(TransitionError):
        await tasks.transition(task.id, TaskStatus.QUEUED)


@pytest.mark.asyncio
async def test_memory_search(database, settings, project) -> None:
    conversations = ConversationService(database, settings)
    memory = await conversations.add_memory(
        "project", "проект использует Wayland и Niri", project_id=project.id
    )
    found = await conversations.search_memories("Wayland")
    assert any(item.id == memory.id for item in found)
