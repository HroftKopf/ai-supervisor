from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_supervisor.core.interfaces import IncomingMessage
from ai_supervisor.models import IntentKind, ReviewVerdict, TaskStatus


async def _run_started_job(orchestrator, timeout: float = 15.0) -> None:
    await asyncio.sleep(0)
    jobs = list(orchestrator._jobs.values())
    assert jobs, "task job was not started"
    await asyncio.wait_for(jobs[0], timeout=timeout)


@pytest.mark.asyncio
async def test_chat_does_not_start_task(orchestrator, database) -> None:
    orch, supervisor, _workers, notifier, _projects, tasks = orchestrator
    supervisor.intent = IntentKind.CHAT
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Как думаешь, стоит ли это менять?")
    )
    assert not orch._jobs
    assert any("ответ Kimi" in item[1] for item in notifier.messages)
    assert await tasks.active() == []


@pytest.mark.asyncio
async def test_task_review_accept_merge_and_persist(orchestrator, database, repo: Path) -> None:
    orch, _supervisor, workers, notifier, _projects, tasks = orchestrator
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Добавь строку в README")
    )
    await _run_started_job(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.WAITING_APPROVAL
    assert task.report and "ожидает принятия" in task.report
    assert task.head_commit and task.head_commit != task.base_commit
    assert any(buttons for _chat, _text, buttons in notifier.messages)
    assert (repo / "README.md").read_text().strip() == "hello"

    await orch.accept_task(1, 424242, 424242)
    task = await tasks.get(1)
    assert task.status is TaskStatus.COMPLETED
    assert task.report and "завершена" in task.report
    assert "verified line" in (repo / "README.md").read_text()
    assert workers.calls == 1
    assert not Path(task.worktree_path or "").exists()
    # Usage was recorded for Kimi plan/review and the fake worker execution.
    rows = await database.fetchall("SELECT provider, requests FROM usage_events")
    assert {str(row["provider"]) for row in rows} == {"kimi", "deepseek"}


@pytest.mark.asyncio
async def test_rework_loop_stops_at_limit(orchestrator, settings) -> None:
    orch, supervisor, workers, notifier, _projects, tasks = orchestrator
    settings.limits.max_rework = 1
    supervisor.review_verdict = ReviewVerdict.REWORK
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Сделай изменение, которое не примут")
    )
    await _run_started_job(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.FAILED
    assert task.rework_count == 1
    assert workers.calls == 2
    assert any("Исчерпан лимит доработок" in text for _chat, text, _buttons in notifier.messages)


@pytest.mark.asyncio
async def test_empty_diff_is_never_accepted(orchestrator, settings) -> None:
    orch, _supervisor, workers, _notifier, _projects, tasks = orchestrator
    settings.limits.max_rework = 0
    workers.write_content = None
    await orch.handle_incoming(
        IncomingMessage(
            chat_id=424242, user_id=424242, text="Ничего не меняй, но задача требует изменения"
        )
    )
    await _run_started_job(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.FAILED
    assert task.review is not None
    assert task.review.verdict is ReviewVerdict.REWORK


@pytest.mark.asyncio
async def test_stop_cancels_running_worker(orchestrator) -> None:
    orch, _supervisor, workers, _notifier, _projects, tasks = orchestrator
    workers.block = True
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Запусти долгую работу")
    )
    await asyncio.wait_for(workers.started.wait(), timeout=10)
    await orch.stop_task(1, 424242)
    await asyncio.sleep(0.2)
    task = await tasks.get(1)
    assert task.status is TaskStatus.CANCELLED
    assert workers.cancelled


@pytest.mark.asyncio
async def test_rejects_second_task_while_waiting_approval(orchestrator) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Первая задача")
    )
    await _run_started_job(orch)
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Вторая задача")
    )
    assert len(await tasks.list(limit=10)) == 1
    assert (await tasks.get(1)).status is TaskStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_rollback_requires_approval_and_saves_patch(orchestrator, settings) -> None:
    orch, _supervisor, _workers, notifier, _projects, tasks = orchestrator
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Сделай изменение для отката")
    )
    await _run_started_job(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.WAITING_APPROVAL
    await orch.request_rollback(1, 424242, 424242)
    approval_id = None
    for _chat, _text, buttons in reversed(notifier.messages):
        if not buttons:
            continue
        for row in buttons:
            for button in row:
                if button.callback_data.startswith("apv:"):
                    approval_id = int(button.callback_data.split(":", 1)[1])
                    break
        if approval_id is not None:
            break
    assert approval_id is not None
    await orch.approve_action(approval_id, 424242, 424242)
    task = await tasks.get(1)
    assert task.status is TaskStatus.CANCELLED
    assert task.worktree_path and not Path(task.worktree_path).exists()
    assert list(settings.rollbacks_dir.glob("task-0001-*.patch"))
