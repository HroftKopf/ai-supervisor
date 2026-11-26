from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_supervisor.core.worker_manager import WorkerError, WorkerManager
from ai_supervisor.memory.tasks import TaskService


class FakeSandbox:
    def worker_argv(self, paths, **kwargs):  # type: ignore[no-untyped-def]
        return [
            sys.executable,
            str(Path(__file__).with_name("fake_worker.py")),
            self.mode,
            str(paths.state_dir),
        ]


class FakeWorkspace:
    def __init__(self) -> None:
        self.mode = "success"

    @staticmethod
    def extra_read_paths(project):  # type: ignore[no-untyped-def]
        return ()

    def sandbox_paths(self, worktree, dsh_home, state_dir, readonly_roots=()):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            worktree=worktree,
            dsh_home=dsh_home,
            state_dir=state_dir,
            git_common_dir=None,
            git_worktree_name=None,
            readonly_roots=readonly_roots,
        )


async def _setup(tmp_path: Path, settings, mode: str = "success"):
    db = __import__("ai_supervisor.memory.database", fromlist=["Database"]).Database(
        tmp_path / "db.sqlite"
    )
    await db.connect()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.task_logs_dir.mkdir(parents=True, exist_ok=True)
    projects = __import__(
        "ai_supervisor.memory.projects", fromlist=["ProjectService"]
    ).ProjectService(db, settings)
    conversations = __import__(
        "ai_supervisor.memory.conversations", fromlist=["ConversationService"]
    ).ConversationService(db, settings)
    root = tmp_path / "projects"
    root.mkdir(exist_ok=True)
    settings.security.allowed_project_roots = [str(root)]
    project = await projects.create("demo")
    conversation = await conversations.get_or_create_conversation(424242, project.id)
    tasks = TaskService(db)
    task = await tasks.create(project.id, conversation.id, "worker", max_rework=1)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = await tasks.update_fields(task.id, worktree_path=str(worktree))
    workspace = FakeWorkspace()
    workspace.mode = mode
    sandbox = FakeSandbox()
    sandbox.mode = mode
    manager = WorkerManager(
        settings=settings,
        sandbox=sandbox,
        workspace=workspace,
        tasks=tasks,
        redactor=__import__(
            "ai_supervisor.core.redaction", fromlist=["SecretRedactor"]
        ).SecretRedactor([]),
    )
    return db, tasks, task, project, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "nonzero", "corrupt"])
async def test_worker_result_modes(tmp_path: Path, settings, mode: str) -> None:
    db, _tasks, task, project, manager = await _setup(tmp_path, settings, mode)
    try:
        result = await manager.run(
            task=task,
            project=project,
            prompt="hello",
            attempt=1,
            resume=False,
            timeout_seconds=5,
        )
        if mode == "success":
            assert result.completed and result.final_response == "done"
        else:
            assert not result.completed
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_worker_timeout_terminates_process_group(tmp_path: Path, settings) -> None:
    db, _tasks, task, project, manager = await _setup(tmp_path, settings, "hang")
    try:
        result = await manager.run(
            task=task, project=project, prompt="hello", attempt=1, resume=False, timeout_seconds=0.2
        )
        assert result.timed_out
        assert not manager.is_running(task.id)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_worker_cancel_terminates_group(tmp_path: Path, settings) -> None:
    db, _tasks, task, project, manager = await _setup(tmp_path, settings, "hang")
    try:
        runner = asyncio.create_task(
            manager.run(
                task=task,
                project=project,
                prompt="hello",
                attempt=1,
                resume=False,
                timeout_seconds=10,
            )
        )
        for _ in range(100):
            if manager.is_running(task.id):
                break
            await asyncio.sleep(0.02)
        assert manager.is_running(task.id)
        assert await manager.cancel(task.id) is True
        result = await asyncio.wait_for(runner, timeout=5)
        assert not manager.is_running(task.id)
        assert result.return_code is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancelled_before_start_is_rejected(tmp_path: Path, settings) -> None:
    db, _tasks, task, project, manager = await _setup(tmp_path, settings, "success")
    try:
        manager.clear_cancel(task.id)
        await manager.cancel(task.id)
        with pytest.raises(WorkerError, match="отменена"):
            await manager.run(
                task=task, project=project, prompt="x", attempt=1, resume=False, timeout_seconds=1
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_worker_kills_child_in_same_process_group(tmp_path: Path, settings) -> None:
    db, _tasks, task, project, manager = await _setup(tmp_path, settings, "child")
    try:
        result = await manager.run(
            task=task, project=project, prompt="hello", attempt=1, resume=False, timeout_seconds=0.3
        )
        assert result.timed_out
        child_pid = int(
            (tmp_path / "var" / "logs" / "task-0001" / "attempt-1" / "child.pid").read_text()
        )
        for _ in range(50):
            if not Path(f"/proc/{child_pid}").exists():
                break
            await asyncio.sleep(0.02)
        assert not Path(f"/proc/{child_pid}").exists()
    finally:
        await db.close()
