from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_supervisor.core.interfaces import IncomingMessage
from ai_supervisor.models import (
    RiskLevel,
    StageExecutor,
    StageOperation,
    StageSpec,
    StageStatus,
    TaskSpec,
    TaskStatus,
    TaskType,
)


async def _run_until_idle(orch, limit: int = 20) -> None:
    for _ in range(limit):
        jobs = [job for job in orch._jobs.values() if not job.done()]
        if not jobs:
            await asyncio.sleep(0)
            jobs = [job for job in orch._jobs.values() if not job.done()]
            if not jobs:
                return
        await asyncio.gather(*jobs, return_exceptions=True)


def _composite_spec(source: Path) -> TaskSpec:
    return TaskSpec(
        title="Импортировать Niri и закоммитить",
        goal="Импортировать проверенную конфигурацию Niri в проект и создать коммит",
        task_type=TaskType.COMPOSITE,
        source_request="Залей мою конфигурацию Niri в проект",
        acceptance_criteria=["конфигурация импортирована", "изменения закоммичены"],
        allowed_paths=["niri"],
        expected_artifacts=["niri/config.kdl"],
        requires_file_changes=True,
        stages=[
            StageSpec(
                id="import",
                operation=StageOperation.IMPORT_CONFIG,
                goal="Скопировать Niri config в worktree",
                executor=StageExecutor.PYTHON,
                required_permissions=[RiskLevel.HIGH],
                acceptance_criteria=["destination exists"],
                inputs=[],
                outputs=["niri/config.kdl"],
                source=str(source),
                destination="niri/config.kdl",
            ),
            StageSpec(
                id="commit",
                operation=StageOperation.GIT_COMMIT,
                goal="Закоммитить импортированный файл",
                depends_on=["import"],
                executor=StageExecutor.GIT,
                required_permissions=[RiskLevel.LOW],
                acceptance_criteria=["commit exists"],
                inputs=["niri/config.kdl"],
                outputs=["commit"],
                commit_message="import niri config",
            ),
            StageSpec(
                id="check",
                operation=StageOperation.VALIDATE,
                goal="Проверить diff и отсутствие секретов",
                depends_on=["commit"],
                executor=StageExecutor.VALIDATOR,
                required_permissions=[RiskLevel.LOW],
                acceptance_criteria=["validation passes"],
                inputs=["commit"],
                outputs=["validation"],
                allowed_paths=["niri"],
                require_change=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_composite_import_commit_validate_autocontinues(
    orchestrator, project, tmp_path: Path
) -> None:
    orch, supervisor, _workers, notifier, projects, tasks = orchestrator
    source = tmp_path / "niri-config"
    source.mkdir()
    (source / "config.kdl").write_text("layout {\n  dwindle\n}\n", encoding="utf-8")
    await projects.update_instructions(project.id, extra_read_paths=[str(source)])
    supervisor.plan_spec = _composite_spec(source / "config.kdl")
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Залей мою конфигурацию Niri")
    )
    await _run_until_idle(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.WAITING_APPROVAL
    stages = await orch.stages.list(task.id)
    assert [stage.stage_key for stage in stages] == ["import", "commit", "check"]
    assert stages[0].status is StageStatus.WAITING_APPROVAL
    # Import is a high-risk stage and must not run before approval.
    assert not (Path(task.worktree_path or "") / "niri/config.kdl").exists()

    approval_id = None
    for _chat, _text, buttons in reversed(notifier.messages):
        for row in buttons or []:
            for button in row:
                if button.callback_data.startswith("apv:"):
                    approval_id = int(button.callback_data.split(":", 1)[1])
                    break
        if approval_id is not None:
            break
    assert approval_id is not None
    await orch.approve_action(approval_id, 424242, 424242)
    await _run_until_idle(orch)

    task = await tasks.get(1)
    assert task.status is TaskStatus.COMPLETED
    stages = await orch.stages.list(task.id)
    assert all(stage.status is StageStatus.COMPLETED for stage in stages)
    assert (
        Path(task.worktree_path or "") / "niri/config.kdl"
    ).read_text() == "layout {\n  dwindle\n}\n"
    assert task.head_commit
    assert task.report and "## Этапы" in task.report
    assert supervisor.chat_calls == 0
    assert supervisor.review_calls == 0


@pytest.mark.asyncio
async def test_composite_stage_rejection_blocks_and_preserves_state(
    orchestrator, project, tmp_path: Path
) -> None:
    orch, supervisor, _workers, notifier, projects, tasks = orchestrator
    source = tmp_path / "niri-config"
    source.mkdir()
    (source / "config.kdl").write_text("layout {}\n", encoding="utf-8")
    await projects.update_instructions(project.id, extra_read_paths=[str(source)])
    supervisor.plan_spec = _composite_spec(source / "config.kdl")
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Импортируй Niri")
    )
    await _run_until_idle(orch)
    task = await tasks.get(1)
    approval_id = None
    for _chat, _text, buttons in reversed(notifier.messages):
        for row in buttons or []:
            for button in row:
                if button.callback_data.startswith("rej:"):
                    approval_id = int(button.callback_data.split(":", 1)[1])
                    break
        if approval_id is not None:
            break
    assert approval_id is not None
    await orch.reject_action(approval_id, 424242, 424242)
    task = await tasks.get(1)
    assert task.status is TaskStatus.BLOCKED
    stages = await orch.stages.list(task.id)
    assert stages[0].status is StageStatus.BLOCKED
    assert not (Path(task.worktree_path or "") / "niri/config.kdl").exists()


def _niri_spec(source: Path, repo_name: str) -> TaskSpec:
    return TaskSpec(
        title="Niri to GitHub",
        goal="Импортировать Niri config, проверить, создать репозиторий и опубликовать",
        task_type=TaskType.COMPOSITE,
        source_request="Залей мою конфигурацию Niri на GitHub",
        acceptance_criteria=["конфигурация опубликована"],
        allowed_paths=["niri"],
        expected_artifacts=["niri/config.kdl"],
        stages=[
            StageSpec(
                id="import",
                operation=StageOperation.IMPORT_CONFIG,
                goal="Импортировать Niri",
                executor=StageExecutor.PYTHON,
                required_permissions=[RiskLevel.HIGH],
                acceptance_criteria=["file imported"],
                destination="niri/config.kdl",
            ),
            StageSpec(
                id="commit",
                operation=StageOperation.GIT_COMMIT,
                goal="Создать коммит",
                depends_on=["import"],
                executor=StageExecutor.GIT,
                required_permissions=[RiskLevel.LOW],
                acceptance_criteria=["commit exists"],
                commit_message="import niri",
            ),
            StageSpec(
                id="check",
                operation=StageOperation.VALIDATE,
                goal="Проверить diff",
                depends_on=["commit"],
                executor=StageExecutor.VALIDATOR,
                required_permissions=[RiskLevel.LOW],
                acceptance_criteria=["valid"],
                allowed_paths=["niri"],
                require_change=True,
            ),
            StageSpec(
                id="repo",
                operation=StageOperation.GITHUB_ENSURE_REPO,
                goal="Создать GitHub repo",
                depends_on=["check"],
                executor=StageExecutor.GITHUB,
                required_permissions=[RiskLevel.HIGH],
                acceptance_criteria=["repo exists"],
                repo_name=repo_name,
                private=True,
            ),
            StageSpec(
                id="publish",
                operation=StageOperation.PUBLISH,
                goal="Опубликовать base branch",
                depends_on=["repo"],
                executor=StageExecutor.GIT,
                required_permissions=[RiskLevel.HIGH],
                acceptance_criteria=["remote sha verified"],
                merge=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_niri_composite_publishes_to_github_after_approvals(
    orchestrator, project, tmp_path: Path
) -> None:
    import subprocess

    orch, supervisor, _workers, notifier, projects, tasks = orchestrator
    root = tmp_path / "configs"
    source = root / "niri"
    source.mkdir(parents=True)
    (source / "config.kdl").write_text("layout {}\n", encoding="utf-8")
    remote = tmp_path / "github-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    await projects.update_instructions(project.id, extra_read_paths=[str(root)])

    class FakeGitHub:
        def __init__(self) -> None:
            self.created = 0

        async def authenticated_user(self):
            return {"login": "owner"}

        async def repository_info(self, owner, repo):
            return None

        async def create_repository(self, name, *, private=True, description="", owner=None):
            self.created += 1
            return {
                "full_name": f"owner/{name}",
                "html_url": f"file://{remote}",
                "clone_url": f"file://{remote}",
                "private": private,
            }

        async def close(self):
            return None

    fake = FakeGitHub()
    orch.github = fake  # type: ignore[assignment]
    supervisor.plan_spec = _niri_spec(source / "config.kdl", "niri-config")
    await orch.handle_incoming(
        IncomingMessage(
            chat_id=424242, user_id=424242, text="Залей мою конфигурацию Niri на GitHub"
        )
    )
    await _run_until_idle(orch)

    async def approve_latest() -> None:
        approval_id = None
        for _chat, _text, buttons in reversed(notifier.messages):
            for row in buttons or []:
                for button in row:
                    if button.callback_data.startswith("apv:"):
                        approval_id = int(button.callback_data.split(":", 1)[1])
                        break
            if approval_id is not None:
                break
        assert approval_id is not None, notifier.messages[-1][1]
        await orch.approve_action(approval_id, 424242, 424242)
        await _run_until_idle(orch)

    # import approval, repo creation approval, publish approval
    await approve_latest()
    await approve_latest()
    before_publish = subprocess.run(
        ["git", "ls-remote", "--heads", str(remote), "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert before_publish.stdout.strip() == ""
    await approve_latest()

    task = await tasks.get(1)
    assert task.status is TaskStatus.COMPLETED, task.report
    assert fake.created == 1
    result = subprocess.run(
        ["git", "ls-remote", "--heads", str(remote), "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip()
    assert notifier.messages


@pytest.mark.asyncio
async def test_failed_stage_preserves_completed_stages(
    orchestrator, project, tmp_path: Path
) -> None:
    orch, supervisor, _workers, notifier, projects, tasks = orchestrator
    source = tmp_path / "niri"
    source.mkdir()
    (source / "config.kdl").write_text("layout {}\n", encoding="utf-8")
    await projects.update_instructions(project.id, extra_read_paths=[str(source)])
    spec = _composite_spec(source / "config.kdl")
    spec.stages[2] = spec.stages[2].model_copy(update={"allowed_paths": ["other"]})
    supervisor.plan_spec = spec
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Импортируй и проверь Niri")
    )
    await _run_until_idle(orch)
    approval_id = None
    for _chat, _text, buttons in reversed(notifier.messages):
        for row in buttons or []:
            for button in row:
                if button.callback_data.startswith("apv:"):
                    approval_id = int(button.callback_data.split(":", 1)[1])
                    break
        if approval_id is not None:
            break
    assert approval_id is not None
    await orch.approve_action(approval_id, 424242, 424242)
    await _run_until_idle(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.BLOCKED
    stages = await orch.stages.list(task.id)
    assert stages[0].status is StageStatus.COMPLETED
    assert stages[1].status is StageStatus.COMPLETED
    assert stages[2].status is StageStatus.FAILED
    assert (Path(task.worktree_path or "") / "niri/config.kdl").exists()


@pytest.mark.asyncio
async def test_identical_import_needs_no_fake_change(orchestrator, project, tmp_path: Path) -> None:
    orch, supervisor, _workers, notifier, projects, tasks = orchestrator
    source = tmp_path / "same"
    source.mkdir()
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    await projects.update_instructions(project.id, extra_read_paths=[str(source)])
    spec = TaskSpec(
        title="Same README",
        goal="Импортировать уже совпадающий README без фиктивных изменений",
        task_type=TaskType.COMPOSITE,
        source_request="Импортируй README",
        acceptance_criteria=["README совпадает"],
        allowed_paths=["README.md"],
        requires_file_changes=False,
        stages=[
            StageSpec(
                id="import",
                operation=StageOperation.IMPORT_CONFIG,
                goal="Импортировать README",
                executor=StageExecutor.PYTHON,
                required_permissions=[RiskLevel.HIGH],
                acceptance_criteria=["file exists"],
                source=str(source / "README.md"),
                destination="README.md",
            ),
            StageSpec(
                id="commit",
                operation=StageOperation.GIT_COMMIT,
                goal="Коммит не нужен, но этап должен быть идемпотентным",
                depends_on=["import"],
                executor=StageExecutor.GIT,
                required_permissions=[RiskLevel.LOW],
                acceptance_criteria=["no-op succeeds"],
            ),
            StageSpec(
                id="check",
                operation=StageOperation.VALIDATE,
                goal="Проверить, что фиктивных изменений нет",
                depends_on=["commit"],
                executor=StageExecutor.VALIDATOR,
                required_permissions=[RiskLevel.LOW],
                acceptance_criteria=["validation passes"],
                allowed_paths=["README.md"],
                require_change=False,
            ),
        ],
    )
    supervisor.plan_spec = spec
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Импортируй README")
    )
    await _run_until_idle(orch)
    approval_id = None
    for _chat, _text, buttons in reversed(notifier.messages):
        for row in buttons or []:
            for button in row:
                if button.callback_data.startswith("apv:"):
                    approval_id = int(button.callback_data.split(":", 1)[1])
                    break
        if approval_id is not None:
            break
    assert approval_id is not None
    await orch.approve_action(approval_id, 424242, 424242)
    await _run_until_idle(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.COMPLETED
    stages = await orch.stages.list(task.id)
    assert all(stage.status is StageStatus.COMPLETED for stage in stages)


@pytest.mark.asyncio
async def test_stage_recovery_preserves_completed_and_resets_running(database, project) -> None:
    # Use the same DB services directly; no orchestrator needed for state semantics.
    from ai_supervisor.config import load_settings
    from ai_supervisor.memory.conversations import ConversationService
    from ai_supervisor.memory.stages import TaskStageService
    from ai_supervisor.memory.tasks import TaskService

    current_settings = load_settings(check_env_permissions=False)
    conversations = ConversationService(database, current_settings)
    conversation = await conversations.get_or_create_conversation(424242, project.id)
    tasks = TaskService(database)
    task = await tasks.create(project.id, conversation.id, "recovery", max_rework=3)
    stages = TaskStageService(database)
    specs = [
        StageSpec(
            id="one",
            operation=StageOperation.GIT_COMMIT,
            goal="one",
            executor=StageExecutor.GIT,
            acceptance_criteria=["ok"],
        ),
        StageSpec(
            id="two",
            operation=StageOperation.VALIDATE,
            goal="two",
            depends_on=["one"],
            executor=StageExecutor.VALIDATOR,
            acceptance_criteria=["ok"],
        ),
    ]
    records = await stages.ensure(task.id, specs)
    await stages.set_status(records[0].id, StageStatus.RUNNING)
    await stages.set_status(records[0].id, StageStatus.COMPLETED, result={"ok": True})
    await stages.set_status(records[1].id, StageStatus.RUNNING)
    assert await stages.reset_running(task.id) == 1
    after = await stages.list(task.id)
    assert after[0].status is StageStatus.COMPLETED
    assert after[1].status is StageStatus.PENDING


def test_stage_graph_rejects_cycles_and_wrong_executor() -> None:
    from pydantic import ValidationError

    common = dict(
        title="graph",
        goal="validate graph",
        task_type=TaskType.COMPOSITE,
        acceptance_criteria=["ok"],
    )
    with pytest.raises(ValidationError):
        TaskSpec(
            **common,
            stages=[
                StageSpec(
                    id="a",
                    operation=StageOperation.GIT_COMMIT,
                    goal="a",
                    depends_on=["b"],
                    executor=StageExecutor.GIT,
                    acceptance_criteria=["ok"],
                ),
                StageSpec(
                    id="b",
                    operation=StageOperation.VALIDATE,
                    goal="b",
                    depends_on=["a"],
                    executor=StageExecutor.VALIDATOR,
                    acceptance_criteria=["ok"],
                ),
            ],
        )
    with pytest.raises(ValidationError):
        StageSpec(
            id="bad",
            operation=StageOperation.PUBLISH,
            goal="bad",
            executor=StageExecutor.PYTHON,
            acceptance_criteria=["ok"],
        )


@pytest.mark.asyncio
async def test_auto_resume_after_restart_keeps_completed_stage(
    orchestrator, project, tmp_path: Path
) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await tasks.create(project.id, conversation.id, "resume", max_rework=3)
    spec = TaskSpec(
        title="Resume composite",
        goal="Продолжить составную задачу после перезапуска",
        task_type=TaskType.COMPOSITE,
        acceptance_criteria=["completed"],
        requires_file_changes=False,
        stages=[
            StageSpec(
                id="commit",
                operation=StageOperation.GIT_COMMIT,
                goal="no-op commit",
                executor=StageExecutor.GIT,
                acceptance_criteria=["ok"],
            ),
            StageSpec(
                id="validate",
                operation=StageOperation.VALIDATE,
                goal="validate",
                depends_on=["commit"],
                executor=StageExecutor.VALIDATOR,
                acceptance_criteria=["ok"],
                require_change=False,
            ),
        ],
    )
    task = await tasks.save_spec(task.id, spec)
    task = await tasks.transition(task.id, TaskStatus.PLANNING)
    task = await tasks.transition(task.id, TaskStatus.QUEUED)
    task = await tasks.transition(task.id, TaskStatus.RUNNING)
    prepared = await orch.workspace.prepare(task, project)
    task = await tasks.update_fields(
        task.id,
        worktree_path=prepared["worktree_path"],
        branch=prepared["branch"],
        base_ref=prepared["base_ref"],
        base_commit=prepared["base_commit"],
        head_commit=prepared["base_commit"],
        dsh_home=str(orch.settings.dsh_homes_dir / f"task-{task.id:04d}"),
    )
    stages = await orch.stages.ensure(task.id, spec.stages)
    await orch.stages.set_status(stages[0].id, StageStatus.RUNNING)
    await orch.stages.set_status(stages[0].id, StageStatus.COMPLETED, result={"commit": "x"})
    await orch.stages.set_status(stages[1].id, StageStatus.RUNNING)

    await orch.startup()
    await _run_until_idle(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.COMPLETED
    stages = await orch.stages.list(task.id)
    assert stages[0].status is StageStatus.COMPLETED
    assert stages[1].status is StageStatus.COMPLETED
