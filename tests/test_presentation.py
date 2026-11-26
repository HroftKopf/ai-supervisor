from __future__ import annotations

from pathlib import Path

import pytest

from ai_supervisor.models import (
    Evidence,
    ReviewResult,
    StageExecutor,
    StageOperation,
    StageRecord,
    StageStatus,
    TaskSpec,
    TaskStatus,
    TaskType,
    WorkerResult,
)
from ai_supervisor.presentation.renderer import (
    approval_card,
    completion_card,
    details_view,
    error_card,
    progress_card,
)


def _stages() -> list[StageRecord]:
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    return [
        StageRecord(
            id=1,
            task_id=1,
            stage_key="import",
            position=0,
            operation=StageOperation.IMPORT_CONFIG,
            executor=StageExecutor.PYTHON,
            goal="Импорт конфигурации",
            status=StageStatus.COMPLETED,
            result={"secret_scan": "passed", "files": 1},
            created_at=now,
            updated_at=now,
            started_at=now,
            finished_at=now,
        ),
        StageRecord(
            id=2,
            task_id=1,
            stage_key="commit",
            position=1,
            operation=StageOperation.GIT_COMMIT,
            executor=StageExecutor.GIT,
            goal="Создание коммита",
            status=StageStatus.COMPLETED,
            result={"commit": "a" * 40},
            created_at=now,
            updated_at=now,
            started_at=now,
            finished_at=now,
        ),
        StageRecord(
            id=3,
            task_id=1,
            stage_key="publish",
            position=2,
            operation=StageOperation.PUBLISH,
            executor=StageExecutor.GIT,
            goal="Публикация",
            status=StageStatus.RUNNING,
            created_at=now,
            updated_at=now,
            started_at=now,
        ),
    ]


def _spec() -> TaskSpec:
    return TaskSpec(
        title="Публикация конфигурации",
        goal="Опубликовать конфигурацию в GitHub",
        task_type=TaskType.COMPOSITE,
        acceptance_criteria=["опубликовано"],
        stages=[
            {
                "id": "publish",
                "operation": "publish",
                "goal": "publish",
                "executor": "git",
                "acceptance_criteria": ["ok"],
            }
        ],
    )


def test_progress_card_counts_and_escapes(project, repo: Path) -> None:
    from ai_supervisor.models import TaskRecord

    task = TaskRecord(
        id=4,
        project_id=project.id,
        conversation_id=1,
        title="<b>Niri</b>",
        status=TaskStatus.RUNNING,
        task_type=TaskType.COMPOSITE,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    text = progress_card(task, project, _stages())
    assert "2/3" in text
    assert "✓" in text and "→" in text
    assert "&lt;b&gt;Niri&lt;/b&gt;" in text
    assert "<b>Niri</b>" not in text


def test_completion_card_does_not_claim_unrun_checks(project) -> None:
    from ai_supervisor.models import TaskRecord

    task = TaskRecord(
        id=4,
        project_id=project.id,
        conversation_id=1,
        title="Niri",
        status=TaskStatus.COMPLETED,
        task_type=TaskType.COMPOSITE,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        finished_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    evidence = Evidence(
        task_id=4,
        changed_files=["niri/config.kdl"],
        worker=WorkerResult(session_id="x", finish_reason="completed", return_code=0),
    )
    text = completion_card(
        task,
        project,
        evidence=evidence,
        review=ReviewResult(verdict="approved", summary="Опубликовано."),
        stages=_stages(),
        model_requests=1,
    )
    assert "Проверки: не запускались" in text
    assert "OK" not in text
    assert "1/5" not in text


def test_error_card_hides_traceback_in_compact(project) -> None:
    from ai_supervisor.models import TaskRecord

    task = TaskRecord(
        id=4,
        project_id=project.id,
        conversation_id=1,
        title="Niri",
        status=TaskStatus.BLOCKED,
        error="Traceback: RuntimeError: secret technical detail",
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    text = error_card(task, project, reason=task.error)
    assert "Traceback" not in text
    assert "технической ошибкой" in text or "Выполненные этапы" in text


def test_details_view_includes_commit_and_stage_status(project) -> None:
    from ai_supervisor.models import TaskRecord

    task = TaskRecord(
        id=4,
        project_id=project.id,
        conversation_id=1,
        title="Niri",
        status=TaskStatus.COMPLETED,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    text = details_view(task, project, _spec(), _stages(), None)
    assert "a" * 12 in text
    assert "secret-scan" in text
    assert "import" in text


def test_approval_card_contains_target_and_consequences() -> None:
    text = approval_card(
        title="Публикация",
        why="Опубликовать main",
        target="github.com/owner/repo",
        consequences="Без force",
        volume="1 файл",
    )
    assert "github.com/owner/repo" in text
    assert "Без force" in text
    assert "1 файл" in text


@pytest.mark.asyncio
async def test_progress_manager_edits_existing_and_recovers_deleted(orchestrator, project) -> None:
    orch, _supervisor, _workers, notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await tasks.create(project.id, conversation.id, "progress", max_rework=2)
    task = await tasks.update_fields(task.id, ui_chat_id=424242, ui_message_id=999)
    await orch.progress.update(task.id, force=True)
    assert notifier.edits
    assert notifier.edits[-1][1] == 999
    task = await tasks.get(task.id)
    old_id = task.ui_message_id
    notifier.fail_edits = True
    await orch.progress.update(task.id, force=True)
    task = await tasks.get(task.id)
    assert task.ui_message_id != old_id
    assert notifier.messages


@pytest.mark.asyncio
async def test_ui_mode_persists_and_is_used_by_progress(orchestrator, project) -> None:
    orch, _supervisor, _workers, notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    await orch.conversations.set_ui_mode(424242, "debug")
    assert await orch.conversations.get_ui_mode(424242) == "debug"
    task = await tasks.create(project.id, conversation.id, "mode", max_rework=2)
    task = await tasks.update_fields(task.id, error="technical detail")
    await orch.progress.update(task.id, force=True, chat_id=424242)
    assert any("technical detail" in text for _chat, text, _buttons in notifier.messages)


@pytest.mark.asyncio
async def test_progress_throttling_merges_updates(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    task = await tasks.create(project.id, conversation.id, "throttle", max_rework=2)
    orch.progress.throttle_seconds = 100
    assert await orch.progress.update(task.id, force=True, chat_id=424242) is True
    assert await orch.progress.update(task.id, chat_id=424242) is False
    await orch.progress.close()
