from __future__ import annotations

import asyncio

import pytest

from ai_supervisor.core.permissions import Action, PermissionDenied
from ai_supervisor.memory.revisions import RevisionConflict, RevisionService
from ai_supervisor.models import TaskStatus


async def _paused_task(
    tasks, project, conversation, *, request_id="req-amend", source="original requirements"
):
    task = await tasks.create(
        project.id,
        conversation.id,
        "amendable task",
        max_rework=1,
        source_request=source,
        request_id=request_id,
    )
    await tasks.transition(task.id, TaskStatus.PLANNING)
    await tasks.transition(task.id, TaskStatus.PAUSED)
    return await tasks.get(task.id)


@pytest.mark.asyncio
async def test_amendment_requires_approval_and_preserves_original(orchestrator, project) -> None:
    orch, supervisor, workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    await orch.request_amendment(task.id, "new effective requirements", 424242, 424242)
    assert (await revisions_count(orch, task.id)) == 1  # initial only; amendment waits for approval
    assert (await tasks.get(task.id)).source_request == "original requirements"
    pending = await orch.approvals.latest_pending(user_id=424242)
    assert pending is not None
    await orch.approve_action(int(pending["id"]), 424242, 424242)
    updated = await tasks.get(task.id)
    assert updated.source_request == "original requirements"
    history = await orch.revisions.history(updated)
    assert [int(row["revision_number"]) for row in history] == [1, 2]
    assert history[1]["owner_amendment"] == "new effective requirements"
    assert history[1]["previous_revision_id"] == history[0]["id"]
    assert updated.request_revision == 2
    assert supervisor.plan_calls == []
    assert workers.calls == 0


async def revisions_count(orch, task_id: int) -> int:
    return int(
        await orch.tasks.db.scalar(
            "SELECT COUNT(*) FROM request_revisions WHERE task_id=?", (task_id,)
        )
    )


@pytest.mark.asyncio
async def test_planner_uses_active_effective_revision(orchestrator, project) -> None:
    orch, supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation, source="original requirements")
    await orch.request_amendment(task.id, "amended effective requirements", 424242, 424242)
    pending = await orch.approvals.latest_pending(user_id=424242)
    await orch.approve_action(int(pending["id"]), 424242, 424242)
    task = await tasks.get(task.id)
    planned = await orch._plan_task(task, project)
    assert planned.source_request == "original requirements"
    assert planned.spec is not None and planned.spec.request_revision == 2
    assert supervisor.plan_calls[-1]["latest_user_message"] == "amended effective requirements"


@pytest.mark.asyncio
async def test_amendment_invalidates_spec_review_evidence_and_approvals(
    orchestrator, project
) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    spec = orch._spec_for(task) or __import__(
        "ai_supervisor.models", fromlist=["TaskSpec"]
    ).TaskSpec(title="old plan", goal="old plan goal", acceptance_criteria=["old"])
    task = await tasks.update_fields(
        task.id,
        spec_json=spec.model_dump_json(),
        review_json='{"verdict":"accept","summary":"old"}',
        evidence_json='{"old":true}',
        evidence_manifest_json='{"old":true}',
    )
    await orch.approvals.request(
        action=Action.GIT_PUSH,
        payload={"task_id": task.id},
        created_by=424242,
        task_id=task.id,
    )
    await orch.request_amendment(task.id, "replacement requirements", 424242, 424242)
    pending = await orch.approvals.latest_pending(user_id=424242)
    await orch.approve_action(int(pending["id"]), 424242, 424242)
    updated = await tasks.get(task.id)
    assert updated.spec is None
    assert updated.review is None
    assert updated.evidence_json is None
    assert updated.evidence_manifest_json is None
    rows = await orch.tasks.db.fetchall(
        "SELECT status FROM approvals WHERE task_id=? ORDER BY id", (task.id,)
    )
    assert any(row["status"] == "revoked" for row in rows)


@pytest.mark.asyncio
async def test_amendment_rejects_running_and_completed_tasks(orchestrator, project) -> None:
    orch, _supervisor, workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    await tasks.transition(task.id, TaskStatus.QUEUED)
    await tasks.transition(task.id, TaskStatus.RUNNING)
    await orch.request_amendment(task.id, "must not apply", 424242, 424242)
    assert await orch.approvals.latest_pending(user_id=424242) is None
    assert int(await orch.tasks.db.scalar("SELECT COUNT(*) FROM request_revisions")) == 0
    assert workers.calls == 0


@pytest.mark.asyncio
async def test_amendment_requires_owner(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    with pytest.raises(PermissionDenied):
        await orch.request_amendment(task.id, "hijack", 1, 424242)


@pytest.mark.asyncio
async def test_old_approval_cannot_apply_after_revision_change(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    await orch.request_amendment(task.id, "first amendment", 424242, 424242)
    first = await orch.approvals.latest_pending(user_id=424242)
    await orch.approve_action(int(first["id"]), 424242, 424242)
    await orch.request_amendment(task.id, "second amendment", 424242, 424242)
    # Simulate an old payload based on the previous revision.
    task_now = await tasks.get(task.id)
    with pytest.raises(RevisionConflict):
        await orch.revisions.apply_amendment(
            task=task_now,
            requirements="first amendment",
            author_id=424242,
            expected_revision=1,
            expected_fingerprint=__import__(
                "ai_supervisor.memory.revisions", fromlist=["fingerprint"]
            ).fingerprint("first amendment"),
        )
    assert (await tasks.get(task.id)).request_revision == 2


@pytest.mark.asyncio
async def test_concurrent_amendments_create_one_next_revision(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    from ai_supervisor.memory.revisions import fingerprint

    await orch.revisions.active(task)
    task = await tasks.get(task.id)
    results = await asyncio.gather(
        orch.revisions.apply_amendment(
            task=task,
            requirements="concurrent A",
            author_id=424242,
            expected_revision=1,
            expected_fingerprint=fingerprint("concurrent A"),
        ),
        orch.revisions.apply_amendment(
            task=task,
            requirements="concurrent B",
            author_id=424242,
            expected_revision=1,
            expected_fingerprint=fingerprint("concurrent B"),
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, RevisionConflict) for item in results) == 1
    assert (await tasks.get(task.id)).request_revision == 2
    assert int(await orch.tasks.db.scalar("SELECT COUNT(*) FROM request_revisions")) == 2


@pytest.mark.asyncio
async def test_revision_survives_restart_and_legacy_initial_revision(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    legacy = await tasks.create(
        project.id,
        conversation.id,
        "legacy revision",
        max_rework=1,
        source_request="legacy requirements",
    )
    service = RevisionService(orch.tasks.db)
    initial = await service.active(legacy)
    assert initial["origin"] == "initial"
    assert initial["author_id"] is None
    await tasks.transition(legacy.id, TaskStatus.PLANNING)
    await tasks.transition(legacy.id, TaskStatus.PAUSED)
    await orch.request_amendment(legacy.id, "revision after restart", 424242, 424242)
    pending = await orch.approvals.latest_pending(user_id=424242)
    await orch.approve_action(int(pending["id"]), 424242, 424242)
    restarted = RevisionService(orch.tasks.db)
    active = await restarted.active(await tasks.get(legacy.id))
    assert active["revision_number"] == 2
    assert active["effective_requirements"] == "revision after restart"


@pytest.mark.asyncio
async def test_revisions_view_uses_no_llm(orchestrator, project) -> None:
    orch, supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    before_plan = len(supervisor.plan_calls)
    before_chat = supervisor.chat_calls
    text = await orch.revisions_text(task.id, 424242, 424242)
    assert "Revisions" in text and "r1" in text
    assert len(supervisor.plan_calls) == before_plan
    assert supervisor.chat_calls == before_chat


@pytest.mark.asyncio
async def test_sqlite_failure_rolls_back_amendment(orchestrator, project, monkeypatch) -> None:
    from contextlib import asynccontextmanager

    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation)
    await orch.revisions.active(task)

    @asynccontextmanager
    async def broken_transaction():
        raise RuntimeError("sqlite unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(orch.tasks.db, "transaction", broken_transaction)
    from ai_supervisor.memory.revisions import fingerprint

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        await orch.revisions.apply_amendment(
            task=task,
            requirements="must roll back",
            author_id=424242,
            expected_revision=1,
            expected_fingerprint=fingerprint("must roll back"),
        )
    updated = await tasks.get(task.id)
    assert updated.request_revision == 1
    assert int(await orch.tasks.db.scalar("SELECT COUNT(*) FROM request_revisions")) == 1
