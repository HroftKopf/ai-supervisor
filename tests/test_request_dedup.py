from __future__ import annotations

import asyncio

import pytest

from ai_supervisor.core.interfaces import IncomingMessage
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.request_gateway import build_envelope
from ai_supervisor.memory.requests import RequestService


async def _drain(orch) -> None:
    await asyncio.sleep(0)
    for job in list(orch._jobs.values()):
        await job


@pytest.mark.asyncio
async def test_duplicate_telegram_message_creates_one_task(orchestrator, project) -> None:
    orch, supervisor, workers, _notifier, _projects, tasks = orchestrator
    message = IncomingMessage(
        chat_id=424242, user_id=424242, text="do once", telegram_message_id=7001
    )
    await orch.handle_incoming(message)
    await orch.handle_incoming(message)
    await _drain(orch)
    assert len(await tasks.list(project_id=project.id)) == 1
    assert len(supervisor.plan_calls) == 1
    assert workers.calls == 1
    rows = await orch.tasks.db.fetchall("SELECT dedup_key, task_id FROM requests")
    assert len(rows) == 1 and rows[0]["task_id"] == 1


@pytest.mark.asyncio
async def test_same_text_different_message_ids_are_distinct_requests(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    redactor = SecretRedactor([])
    first = build_envelope(
        IncomingMessage(chat_id=424242, user_id=424242, text="same text", telegram_message_id=1),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=redactor,
    )
    second = build_envelope(
        IncomingMessage(chat_id=424242, user_id=424242, text="same text", telegram_message_id=2),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=redactor,
    )
    assert first.dedup_key != second.dedup_key
    service = RequestService(orch.tasks.db)
    assert (await service.capture(first))[1] is True
    assert (await service.capture(second))[1] is True


@pytest.mark.asyncio
async def test_concurrent_identical_capture_has_one_winner(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    envelope = build_envelope(
        IncomingMessage(chat_id=424242, user_id=424242, text="race", telegram_message_id=99),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=SecretRedactor([]),
    )
    service = RequestService(orch.tasks.db)
    results = await asyncio.gather(*(service.capture(envelope) for _ in range(20)))
    assert sum(1 for _row, created in results if created) == 1
    count = await orch.tasks.db.scalar(
        "SELECT COUNT(*) FROM requests WHERE dedup_key=?", (envelope.dedup_key,)
    )
    assert int(count) == 1


@pytest.mark.asyncio
async def test_task_command_persists_source_message_id(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    await orch.create_task_from_text(
        424242, 424242, "explicit task request", source_message_id=8111
    )
    await _drain(orch)
    row = await orch.tasks.db.fetchone("SELECT * FROM requests WHERE source_message_id=8111")
    assert row is not None and row["task_id"] == 1
    task = await tasks.get(1)
    assert task.source_request == "explicit task request"
    assert task.request_id == row["request_id"]


@pytest.mark.asyncio
async def test_processing_request_recovers_after_restart(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    envelope = build_envelope(
        IncomingMessage(
            chat_id=424242, user_id=424242, text="crash boundary", telegram_message_id=8222
        ),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=SecretRedactor([]),
    )
    service = RequestService(orch.tasks.db)
    assert (await service.capture(envelope))[1] is True
    assert (await service.capture(envelope))[1] is False
    await service.recovery_reset()
    row, created = await service.capture(envelope)
    assert created is True and row["status"] == "processing"


@pytest.mark.asyncio
async def test_long_request_is_rejected_without_storage(orchestrator) -> None:
    orch, _supervisor, _workers, notifier, _projects, _tasks = orchestrator
    before = int(await orch.tasks.db.scalar("SELECT COUNT(*) FROM requests"))
    await orch.create_task_from_text(424242, 424242, "x" * 8001)
    after = int(await orch.tasks.db.scalar("SELECT COUNT(*) FROM requests"))
    assert after == before
    assert any("8000" in text for _chat, text, _buttons in notifier.messages)


@pytest.mark.asyncio
async def test_unauthorized_task_creation_is_refused(orchestrator) -> None:
    from ai_supervisor.core.permissions import PermissionDenied

    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    with pytest.raises(PermissionDenied):
        await orch.create_task_from_text(1, 1, "unauthorized")


@pytest.mark.asyncio
async def test_redelivery_after_new_service_reads_existing_task(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    message = IncomingMessage(
        chat_id=424242, user_id=424242, text="restart dedup", telegram_message_id=9333
    )
    await orch.handle_incoming(message)
    await _drain(orch)
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    envelope = build_envelope(
        message,
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=SecretRedactor([]),
    )
    new_service = RequestService(orch.tasks.db)
    row, created = await new_service.capture(envelope)
    assert created is False
    assert row["task_id"] == 1
    assert len(await tasks.list(project_id=project.id)) == 1


@pytest.mark.asyncio
async def test_synthetic_secret_is_redacted_from_request_and_title(orchestrator, settings) -> None:
    from pydantic import SecretStr

    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    value = "synthetic-request-secret"
    orchestrator_secret = settings.secrets.model_copy(update={"kimi_api_key": SecretStr(value)})
    settings.secrets = orchestrator_secret
    orch.redactor = SecretRedactor.from_settings(settings)
    await orch.create_task_from_text(424242, 424242, f"do work {value}", source_message_id=9444)
    await _drain(orch)
    row = await orch.tasks.db.fetchone(
        "SELECT original_request FROM requests WHERE source_message_id=9444"
    )
    task = await tasks.get(1)
    assert value not in (row["original_request"] if row else "")
    assert value not in task.title
