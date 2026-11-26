from __future__ import annotations

import asyncio

import pytest

from ai_supervisor.core.interfaces import IncomingMessage
from ai_supervisor.models import ReviewResult, ReviewVerdict, RootCause, TaskStatus


async def _run(orch) -> None:
    for _ in range(20):
        jobs = [job for job in orch._jobs.values() if not job.done()]
        if not jobs:
            await asyncio.sleep(0)
            jobs = [job for job in orch._jobs.values() if not job.done()]
            if not jobs:
                return
        await asyncio.gather(*jobs, return_exceptions=True)


def test_review_root_cause_contract() -> None:
    review = ReviewResult(
        verdict=ReviewVerdict.REWORK_REQUIRED,
        summary="нужен другой план",
        root_cause=RootCause.PLANNER_ERROR,
    )
    assert review.root_cause is RootCause.PLANNER_ERROR


@pytest.mark.asyncio
async def test_planner_error_does_not_send_executor_to_rework(orchestrator) -> None:
    orch, supervisor, workers, _notifier, _projects, tasks = orchestrator
    supervisor.review_root_cause = RootCause.PLANNER_ERROR
    supervisor.review_verdict = ReviewVerdict.REWORK
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Сделай изменение")
    )
    await _run(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.BLOCKED
    assert workers.calls == 1
    assert "планировщик" in (task.error or "").lower()
