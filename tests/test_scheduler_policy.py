from __future__ import annotations

import asyncio

import pytest

from ai_supervisor.core.permissions import (
    Action,
    ApprovalError,
    ApprovalService,
    PermissionDenied,
    PermissionLevel,
    PermissionManager,
)
from ai_supervisor.core.worker_manager import WorkerScheduler


@pytest.mark.asyncio
async def test_scheduler_limits_concurrency_and_cancels() -> None:
    scheduler = WorkerScheduler(1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def first() -> str:
        started.set()
        await release.wait()
        return "first"

    async def second() -> str:
        return "second"

    task = asyncio.create_task(scheduler.run(1, first))
    await started.wait()
    assert scheduler.active_count == 1
    assert scheduler.available_slots == 0
    waiting = asyncio.create_task(scheduler.run(2, second))
    await asyncio.sleep(0)
    assert scheduler.active_task_ids() == [1]
    assert await scheduler.cancel(1) is True
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.wait_for(waiting, timeout=1) == "second"
    assert scheduler.active_count == 0


def test_policy_levels_deny_dangerous_actions() -> None:
    policy = PermissionManager()
    assert policy.level(Action.APPLY_MERGE) is PermissionLevel.APPROVAL_REQUIRED
    assert policy.level(Action.DISABLE_SECURITY) is PermissionLevel.DENIED
    with pytest.raises(PermissionDenied):
        policy.require_auto(Action.APPLY_MERGE)
    with pytest.raises(PermissionDenied):
        policy.require_allowed(Action.READ_OTHER_SECRETS)


@pytest.mark.asyncio
async def test_approval_is_single_use_and_action_bound(database, settings) -> None:
    service = ApprovalService(database)
    approval_id = await service.grant_once(
        action=Action.APPLY_MERGE,
        payload={"task_id": 5},
        decided_by=424242,
        task_id=None,
    )
    with pytest.raises(ApprovalError):
        await service.consume(approval_id, user_id=424242, action=Action.ROLLBACK)
    payload = await service.consume(approval_id, user_id=424242, action=Action.APPLY_MERGE)
    assert payload == {"task_id": 5}
    with pytest.raises(ApprovalError):
        await service.consume(approval_id, user_id=424242, action=Action.APPLY_MERGE)
