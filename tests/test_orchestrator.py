from __future__ import annotations

import asyncio
import subprocess
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
    assert task.report and "ожидает подтверждения" in task.report
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
    assert task.status is TaskStatus.BLOCKED
    assert task.rework_count == 1
    assert workers.calls == 2
    assert "Исчерпан лимит доработок" in (task.error or "")
    assert any(
        ("Исчерпан лимит" in text or "Не удалось" in text or "Превышен" in text)
        for _chat, text, _buttons in notifier.messages
    )


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
    assert task.status is TaskStatus.BLOCKED
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


@pytest.mark.asyncio
async def test_read_only_task_completes_without_diff(orchestrator) -> None:
    orch, supervisor, workers, notifier, _projects, tasks = orchestrator
    from ai_supervisor.models import TaskSpec, TaskType

    supervisor.plan_spec = TaskSpec(
        title="Audit README",
        goal="Audit README and report facts without changing files",
        task_type=TaskType.READ_ONLY,
        requires_file_changes=False,
        acceptance_criteria=["README is inspected and a factual report is returned"],
    )
    workers.write_content = None
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Проведи аудит README без изменений")
    )
    await _run_started_job(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.WAITING_APPROVAL
    assert task.task_type.value == "read_only"
    assert task.report and "Изменение файлов не требовалось" in task.report
    assert any(
        "аудит" in text.lower() or "audit" in text.lower() for _c, text, _b in notifier.messages
    )


@pytest.mark.asyncio
async def test_high_risk_contract_waits_for_owner_before_worker(orchestrator) -> None:
    orch, supervisor, workers, notifier, _projects, tasks = orchestrator
    from ai_supervisor.models import RiskLevel, TaskSpec

    supervisor.plan_spec = TaskSpec(
        title="High risk change",
        goal="Perform a high risk operation with owner approval",
        required_permissions=[RiskLevel.HIGH],
        acceptance_criteria=["operation is approved"],
    )
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Выполни рискованную операцию")
    )
    await asyncio.sleep(0.1)
    task = await tasks.get(1)
    assert task.status is TaskStatus.WAITING_APPROVAL
    assert workers.calls == 0
    assert any("требуется подтверждение" in text.lower() for _c, text, _b in notifier.messages)


@pytest.mark.asyncio
async def test_unimplemented_privileged_type_fails_closed(orchestrator) -> None:
    orch, supervisor, workers, _notifier, _projects, tasks = orchestrator
    from ai_supervisor.models import TaskSpec, TaskType

    supervisor.plan_spec = TaskSpec(
        title="System operation",
        goal="Change a host service configuration safely",
        task_type=TaskType.SYSTEM_OPERATION,
        acceptance_criteria=["service is verified"],
    )
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Настрой системный сервис")
    )
    await asyncio.sleep(0.1)
    task = await tasks.get(1)
    assert task.status is TaskStatus.BLOCKED
    assert workers.calls == 0


@pytest.mark.asyncio
async def test_accept_is_idempotent_after_manual_merge(orchestrator, repo: Path) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Сделай изменение для merge")
    )
    await _run_started_job(orch)
    task = await tasks.get(1)
    assert task.branch
    subprocess.run(["git", "merge", "--ff-only", task.branch], cwd=repo, check=True)
    await orch.accept_task(1, 424242, 424242)
    task = await tasks.get(1)
    assert task.status is TaskStatus.COMPLETED
    assert "merge уже был выполнен" in task.report


@pytest.mark.asyncio
async def test_approved_git_push_verifies_remote(
    orchestrator, repo: Path, project, tmp_path: Path
) -> None:
    orch, _supervisor, _workers, notifier, projects, _tasks = orchestrator
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    await projects.update_instructions(project.id, remote_url=f"file://{remote}")
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Сделай изменение для push")
    )
    await _run_started_job(orch)
    await orch.accept_task(1, 424242, 424242)
    await orch.request_push(1, 424242, 424242)
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
    result = subprocess.run(
        ["git", "ls-remote", "--heads", str(remote), "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip()


@pytest.mark.asyncio
async def test_rework_accepts_cumulative_diff_without_new_changes(orchestrator) -> None:
    orch, supervisor, workers, _notifier, _projects, tasks = orchestrator
    from ai_supervisor.models import ReviewVerdict

    supervisor.review_sequence = [ReviewVerdict.REWORK, ReviewVerdict.ACCEPT]
    workers.write_sequence = ["first attempt line\n", None]
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="Сделай изменение за две попытки")
    )
    await _run_started_job(orch)
    task = await tasks.get(1)
    assert task.status is TaskStatus.WAITING_APPROVAL
    events = await tasks.events(1)
    assert task.attempts == 2, (
        task.attempts,
        workers.calls,
        [(e["event_type"], e["message"]) for e in events],
    )
    assert workers.calls == 2
    assert task.evidence_json is not None
    from ai_supervisor.models import Evidence

    evidence = Evidence.model_validate_json(task.evidence_json)
    assert evidence.changed_files == ["README.md"]


@pytest.mark.asyncio
async def test_github_repository_create_requires_approval(orchestrator) -> None:
    orch, _supervisor, _workers, notifier, _projects, _tasks = orchestrator

    class FakeGitHub:
        def __init__(self) -> None:
            self.calls = 0

        async def create_repository(
            self, name: str, *, private: bool = True, description: str = ""
        ):
            self.calls += 1
            return {
                "full_name": f"owner/{name}",
                "html_url": f"https://github.com/owner/{name}",
                "private": private,
            }

        async def close(self) -> None:
            return None

    fake = FakeGitHub()
    orch.github = fake  # type: ignore[assignment]
    await orch.request_github_create("new-repo", True, 424242, 424242)
    assert fake.calls == 0
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
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_config_import_requires_owner_approval(orchestrator, project, tmp_path: Path) -> None:
    orch, _supervisor, _workers, notifier, projects, _tasks = orchestrator
    source = tmp_path / "niri.conf"
    source.write_text("layout = 'dwindle'\n", encoding="utf-8")
    await projects.update_instructions(project.id, extra_read_paths=[str(tmp_path)])
    await orch.request_config_import(424242, 424242, str(source), "configs/niri.conf")
    destination = Path(project.path) / "configs/niri.conf"
    assert not destination.exists()
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
    assert destination.read_text() == "layout = 'dwindle'\n"
