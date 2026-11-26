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


@pytest.mark.asyncio
async def test_composite_with_stages_rejects_amendment_and_keeps_stages(
    orchestrator, project
) -> None:
    from ai_supervisor.models import StageExecutor, StageOperation, StageSpec, TaskType

    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation, request_id="req-composite")
    await tasks.update_fields(task.id, task_type=TaskType.COMPOSITE)
    await orch.stages.ensure(
        task.id,
        [
            StageSpec(
                id="one",
                operation=StageOperation.VALIDATE,
                goal="validate stage",
                executor=StageExecutor.VALIDATOR,
                acceptance_criteria=["ok"],
            )
        ],
    )
    before = [(stage.stage_key, stage.status) for stage in await orch.stages.list(task.id)]
    await orch.request_amendment(task.id, "new composite requirements", 424242, 424242)
    assert await orch.approvals.latest_pending(user_id=424242) is None
    assert (await tasks.get(task.id)).request_revision == 1
    after = [(stage.stage_key, stage.status) for stage in await orch.stages.list(task.id)]
    assert after == before and len(after) == 1


@pytest.mark.asyncio
async def test_published_or_merged_task_rejects_amendment(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation, request_id="req-published")
    await tasks.add_event(task.id, "merged", "merged already")
    await orch.request_amendment(task.id, "must not apply", 424242, 424242)
    assert await orch.approvals.latest_pending(user_id=424242) is None
    assert (await tasks.get(task.id)).request_revision == 1


@pytest.mark.asyncio
async def test_completed_and_partially_completed_reject_amendment(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    for index, status in enumerate(("completed", "partially_completed"), start=1):
        task = await _paused_task(
            tasks, project, conversation, request_id=f"req-final-{index}", source="final task"
        )
        await orch.tasks.db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task.id))
        await orch.request_amendment(task.id, "must not apply", 424242, 424242)
        assert await orch.approvals.latest_pending(user_id=424242, task_id=task.id) is None
        assert (await tasks.get(task.id)).request_revision == 1


@pytest.mark.asyncio
async def test_amendment_retires_session_worktree_and_preserves_artifacts(
    orchestrator, project, tmp_path
) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation, request_id="req-session")
    old_logs = tmp_path / "old-logs"
    old_logs.mkdir()
    (old_logs / "history.jsonl").write_text("old session evidence", encoding="utf-8")
    old_worktree = tmp_path / "old-worktree"
    old_worktree.mkdir()
    await tasks.update_fields(
        task.id,
        session_id="old-session-id",
        dsh_home=str(tmp_path / "old-dsh"),
        worktree_path=str(old_worktree),
        branch="old-branch",
        logs_path=str(old_logs),
        diff_path=str(tmp_path / "old.patch"),
    )
    await orch.request_amendment(task.id, "new requirements for new session", 424242, 424242)
    pending = await orch.approvals.latest_pending(user_id=424242)
    await orch.approve_action(int(pending["id"]), 424242, 424242)
    updated = await tasks.get(task.id)
    assert updated.session_id is None
    assert updated.worktree_path is None
    assert updated.logs_path is None
    assert (old_logs / "history.jsonl").read_text() == "old session evidence"
    assert old_worktree.is_dir()
    row = await orch.tasks.db.fetchone(
        "SELECT * FROM task_sessions WHERE task_id=? AND session_id='old-session-id'", (task.id,)
    )
    assert row is not None and row["worktree_path"] == str(old_worktree)
    assert orch.workspace.worktree_path(updated).name.endswith("-r2")
    assert "-r2-" in orch.workspace.branch_name(updated)


@pytest.mark.asyncio
async def test_running_job_or_status_change_rejects_amendment(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation, request_id="req-job")
    job = asyncio.create_task(asyncio.sleep(10))
    orch._jobs[task.id] = job
    try:
        await orch.request_amendment(task.id, "blocked by job", 424242, 424242)
        assert await orch.approvals.latest_pending(user_id=424242) is None
        assert (await tasks.get(task.id)).request_revision == 1
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        orch._jobs.pop(task.id, None)

    # Approval created while paused, then task resumes before confirmation.
    await orch.request_amendment(task.id, "after status change", 424242, 424242)
    pending = await orch.approvals.latest_pending(user_id=424242)
    assert pending is not None
    await tasks.transition(task.id, TaskStatus.QUEUED)
    await orch.approve_action(int(pending["id"]), 424242, 424242)
    assert (await tasks.get(task.id)).request_revision == 1


@pytest.mark.asyncio
async def test_old_approved_external_action_is_revoked_by_amendment(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await _paused_task(tasks, project, conversation, request_id="req-old-approval")
    approval_id = await orch.approvals.grant_once(
        action=Action.GIT_PUSH,
        payload={"task_id": task.id},
        decided_by=424242,
        task_id=task.id,
    )
    await orch.request_amendment(task.id, "revision invalidates push", 424242, 424242)
    pending = await orch.approvals.latest_pending(user_id=424242)
    await orch.approve_action(int(pending["id"]), 424242, 424242)
    old = await orch.approvals.get(approval_id)
    assert old is not None and old["status"] == "revoked"
    from ai_supervisor.core.permissions import ApprovalError

    with pytest.raises(ApprovalError):
        await orch.approvals.consume(approval_id, user_id=424242, action=Action.GIT_PUSH)
