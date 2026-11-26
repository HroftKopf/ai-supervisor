from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ai_supervisor.core.evidence_manifest import build_manifest
from ai_supervisor.core.interfaces import IncomingMessage
from ai_supervisor.models import Evidence, TaskSpec, TaskStatus, WorkerResult


def _evidence(task_id: int) -> Evidence:
    return Evidence(
        task_id=task_id,
        worker=WorkerResult(
            session_id="test-session",
            final_response="done",
            finish_reason="completed",
            return_code=0,
        ),
    )


def _spec(source: str = "evidence task") -> TaskSpec:
    return TaskSpec(
        title="evidence task",
        goal="produce a verifiable evidence snapshot",
        acceptance_criteria=["worktree remains consistent"],
        source_request=source,
    )


async def _task(orchestrator, project):
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    return await tasks.create(
        project.id, conversation.id, "evidence task", max_rework=1, source_request="evidence task"
    )


@pytest.mark.asyncio
async def test_untracked_content_same_size_and_mtime_is_detected(
    orchestrator, project, repo: Path
) -> None:
    task = await _task(orchestrator, project)
    path = repo / "artifact.txt"
    path.write_text("abc", encoding="utf-8")
    first = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=_spec()
    )
    stat = path.stat()
    path.write_text("abd", encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=_spec()
    )
    assert first.untracked_hash != second.untracked_hash
    assert first.manifest_hash != second.manifest_hash


@pytest.mark.asyncio
async def test_nested_untracked_and_symlink_are_fingerprinted_safely(
    orchestrator, project, repo: Path, tmp_path: Path
) -> None:
    task = await _task(orchestrator, project)
    nested = repo / "generated" / "nested"
    nested.mkdir(parents=True)
    (nested / "result.txt").write_text("nested evidence", encoding="utf-8")
    first = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=_spec()
    )
    assert "generated/nested/result.txt" not in first.unsafe_paths
    (nested / "result.txt").write_text("changed nested evidence", encoding="utf-8")
    second = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=_spec()
    )
    assert first.untracked_hash != second.untracked_hash
    external = tmp_path / "outside.txt"
    external.write_text("must not be read", encoding="utf-8")
    (repo / "external-link").symlink_to(external)
    third = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=_spec()
    )
    assert "external-link" in third.unsafe_paths


@pytest.mark.asyncio
async def test_tracked_head_and_validation_config_changes_are_detected(
    orchestrator, project, repo: Path
) -> None:
    task = await _task(orchestrator, project)
    (repo / "README.md").write_text("tracked change", encoding="utf-8")
    first = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=_spec()
    )
    (repo / "README.md").write_text("another change", encoding="utf-8")
    second = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=_spec()
    )
    assert first.tracked_change_hash != second.tracked_change_hash
    changed_spec = _spec().model_copy(update={"acceptance_criteria": ["different checks"]})
    third = await build_manifest(
        project=project, task=task, worktree=repo, evidence=_evidence(task.id), spec=changed_spec
    )
    assert first.validation_hash != third.validation_hash
    assert first.spec_hash != third.spec_hash


async def _wait_task(orchestrator) -> None:
    import asyncio

    await asyncio.sleep(0)
    for job in list(orchestrator._jobs.values()):
        await job


async def _waiting_task(orch, tasks, project):
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="change README for acceptance gate")
    )
    await _wait_task(orch)
    return await tasks.get(1)


@pytest.mark.asyncio
async def test_acceptance_rejects_wrong_manifest_and_dirty_worktree(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    task = await _waiting_task(orch, tasks, project)
    assert task.status is TaskStatus.WAITING_APPROVAL
    worktree = Path(task.worktree_path or "")
    (worktree / "late-artifact.txt").write_text("changed after review", encoding="utf-8")
    await orch.accept_task(task.id, 424242, 424242, expected_manifest_hash="wrong")
    current = await tasks.get(task.id)
    assert current.status is TaskStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_acceptance_rejects_moved_branch_or_base(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    task = await _waiting_task(orch, tasks, project)
    manifest = task.evidence_manifest_json
    assert manifest
    from ai_supervisor.core.evidence_manifest import parse_manifest

    parsed = parse_manifest(manifest)
    assert parsed is not None
    worktree = Path(task.worktree_path or "")
    (worktree / "README.md").write_text("branch move\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "move branch"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )
    await orch.accept_task(task.id, 424242, 424242, expected_manifest_hash=parsed.manifest_hash)
    assert (await tasks.get(task.id)).status is TaskStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_acceptance_rejects_base_branch_move(orchestrator, project, repo: Path) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    task = await _waiting_task(orch, tasks, project)
    from ai_supervisor.core.evidence_manifest import parse_manifest

    parsed = parse_manifest(task.evidence_manifest_json)
    assert parsed is not None
    (repo / "base-move.txt").write_text("base moved\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "base-move.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "move base"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )
    await orch.accept_task(task.id, 424242, 424242, expected_manifest_hash=parsed.manifest_hash)
    assert (await tasks.get(task.id)).status is TaskStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_acceptance_rejects_missing_or_corrupt_manifest(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    task = await _waiting_task(orch, tasks, project)
    await tasks.update_fields(task.id, evidence_manifest_json='{"corrupt": true}')
    await orch.accept_task(task.id, 424242, 424242)
    assert (await tasks.get(task.id)).status is TaskStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_validation_change_between_checks_and_commit_is_detected(
    orchestrator, project, repo: Path, monkeypatch
) -> None:
    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    from ai_supervisor.core.workspace import WorkspaceError

    worktree = repo

    async def mutating_checks(**kwargs):
        (worktree / "validation-change.txt").write_text("changed", encoding="utf-8")
        return []

    monkeypatch.setattr(orch.workspace, "run_checks", mutating_checks)
    task = await _task(orchestrator, project)
    task = await _tasks.get(task.id)
    with pytest.raises(WorkspaceError, match="changed during validation"):
        await orch.workspace.collect_evidence(
            task=task,
            project=project,
            worktree=worktree,
            dsh_home=repo / ".dsh",
            state_dir=repo / ".state",
            worker=_evidence(task.id).worker,
            check_timeout_seconds=10,
            diff_path=repo / ".state" / "diff.patch",
        )


@pytest.mark.asyncio
async def test_review_from_previous_revision_is_rejected(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    task = await _waiting_task(orch, tasks, project)
    review = task.review
    assert review is not None
    stale = review.model_copy(
        update={"request_revision": task.request_revision - 1, "task_id": task.id}
    )
    await tasks.update_fields(task.id, review_json=stale.model_dump_json())
    await orch.accept_task(task.id, 424242, 424242)
    assert (await tasks.get(task.id)).status is TaskStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_missing_manifest_is_rejected(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    task = await _waiting_task(orch, tasks, project)
    await tasks.update_fields(task.id, evidence_manifest_json=None)
    await orch.accept_task(task.id, 424242, 424242)
    assert (await tasks.get(task.id)).status is TaskStatus.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_change_during_commit_blocks_acceptance(orchestrator, project, monkeypatch) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    original = orch.workspace.commit_changes

    async def commit_and_mutate(**kwargs):
        result = await original(**kwargs)
        worktree = Path(kwargs["worktree"])
        (worktree / "after-commit.txt").write_text("changed during commit", encoding="utf-8")
        return result

    monkeypatch.setattr(orch.workspace, "commit_changes", commit_and_mutate)
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="change README during commit")
    )
    await _wait_task(orch)
    task = await tasks.get(1)
    assert task.status is not TaskStatus.WAITING_APPROVAL
    assert "commit" in (task.error or "").lower() or "изменился" in (task.error or "")
