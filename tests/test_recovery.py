from __future__ import annotations

import pytest

from ai_supervisor.memory.conversations import ConversationService
from ai_supervisor.memory.tasks import TaskService
from ai_supervisor.models import TaskStatus


@pytest.mark.asyncio
async def test_startup_recovery_interrupts_active_tasks_only(database, settings, project) -> None:
    conversations = ConversationService(database, settings)
    conversation = await conversations.get_or_create_conversation(424242, project.id)
    tasks = TaskService(database)
    running = await tasks.create(project.id, conversation.id, "running", max_rework=3)
    for status in (TaskStatus.PLANNING, TaskStatus.QUEUED, TaskStatus.RUNNING):
        await tasks.transition(running.id, status)
    paused = await tasks.create(project.id, conversation.id, "paused", max_rework=3)
    await tasks.transition(paused.id, TaskStatus.PLANNING)
    await tasks.transition(paused.id, TaskStatus.PAUSED)

    await tasks.create_worker(
        worker_id="w1", task_id=running.id, kind="deepseek", session_id=None, dsh_home=None
    )
    interrupted = await tasks.mark_interrupted_on_startup()
    assert interrupted == [running.id]
    assert (await tasks.get(running.id)).status is TaskStatus.INTERRUPTED
    assert (await tasks.get(paused.id)).status is TaskStatus.PAUSED
    assert await tasks.mark_workers_interrupted_on_startup() == 1
    worker = (await tasks.workers(running.id))[0]
    assert worker["status"] == "interrupted"
