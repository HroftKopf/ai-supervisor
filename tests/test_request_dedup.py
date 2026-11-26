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


@pytest.mark.asyncio
async def test_concurrent_reclaim_of_captured_request_has_one_winner(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    first = build_envelope(
        IncomingMessage(chat_id=424242, user_id=424242, text="reclaim", telegram_message_id=10101),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=SecretRedactor([]),
    )
    service = RequestService(orch.tasks.db)
    original, created = await service.capture(first)
    assert created is True
    await orch.tasks.db.execute(
        "UPDATE requests SET status='captured' WHERE id=?", (original["id"],)
    )
    envelopes = [
        build_envelope(
            IncomingMessage(
                chat_id=424242, user_id=424242, text="reclaim", telegram_message_id=10101
            ),
            project_id=project.id,
            conversation_id=conversation.id,
            redactor=SecretRedactor([]),
        )
        for _ in range(20)
    ]
    results = await asyncio.gather(*(service.capture(item) for item in envelopes))
    winners = [row for row, won in results if won]
    assert len(winners) == 1
    assert winners[0]["request_id"] == original["request_id"]
    row = await service.get(str(original["request_id"]))
    assert row is not None and row["request_id"] == original["request_id"]


@pytest.mark.asyncio
async def test_reclaim_after_project_switch_uses_stored_project(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    first = build_envelope(
        IncomingMessage(
            chat_id=424242, user_id=424242, text="stay here", telegram_message_id=10202
        ),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=SecretRedactor([]),
        mode="task",
        idempotency_key="telegram:424242:10202:task",
    )
    service = RequestService(orch.tasks.db)
    await service.capture(first)
    await orch.tasks.db.execute(
        "UPDATE requests SET status='captured' WHERE dedup_key=?", (first.dedup_key,)
    )
    other = await orch.projects.create("other-project")
    await orch.conversations.set_active_project(424242, other.id)
    await orch.create_task_from_text(
        424242,
        424242,
        "stay here",
        source_message_id=10202,
        idempotency_key="telegram:424242:10202:task",
    )
    await _drain(orch)
    task = await tasks.get(1)
    assert task.project_id == project.id


@pytest.mark.asyncio
async def test_task_binding_failure_rolls_back_task(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    envelope = build_envelope(
        IncomingMessage(chat_id=424242, user_id=424242, text="rollback", telegram_message_id=10303),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=SecretRedactor([]),
    )
    service = RequestService(orch.tasks.db)
    await service.capture(envelope)
    # Remove the request row to force the binding guard inside the transaction.
    await orch.tasks.db.execute("DELETE FROM requests WHERE request_id=?", (envelope.request_id,))
    with pytest.raises(RuntimeError, match="request binding failed"):
        await service.create_task(
            request_id=envelope.request_id,
            project_id=project.id,
            conversation_id=conversation.id,
            title="rollback",
            max_rework=1,
            source_request="rollback",
        )
    assert int(await orch.tasks.db.scalar("SELECT COUNT(*) FROM tasks")) == 0


@pytest.mark.asyncio
async def test_existing_task_is_recovered_without_new_event(orchestrator, project) -> None:
    orch, _supervisor, _workers, _notifier, _projects, _tasks = orchestrator
    conversation = await orch.conversations.get_or_create_conversation(424242, project.id)
    envelope = build_envelope(
        IncomingMessage(
            chat_id=424242, user_id=424242, text="recover task", telegram_message_id=10404
        ),
        project_id=project.id,
        conversation_id=conversation.id,
        redactor=SecretRedactor([]),
    )
    service = RequestService(orch.tasks.db)
    await service.capture(envelope)
    task, created = await service.create_task(
        request_id=envelope.request_id,
        project_id=project.id,
        conversation_id=conversation.id,
        title="recover task",
        max_rework=1,
        source_request="recover task",
    )
    assert created is True
    second, created_again = await service.create_task(
        request_id=envelope.request_id,
        project_id=project.id,
        conversation_id=conversation.id,
        title="recover task",
        max_rework=1,
        source_request="recover task",
    )
    assert second.id == task.id and created_again is False
    events = await orch.tasks.db.fetchall(
        "SELECT event_type FROM task_events WHERE task_id=? AND event_type='accepted'", (task.id,)
    )
    assert len(events) == 1


@pytest.mark.asyncio
async def test_classifier_quota_marks_request_retryable(orchestrator, project) -> None:
    from ai_supervisor.providers.base import ProviderQuotaError

    orch, supervisor, _workers, _notifier, _projects, _tasks = orchestrator

    async def fail(**kwargs):
        raise ProviderQuotaError("classifier quota")

    supervisor.classify = fail  # type: ignore[method-assign]
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="quota", telegram_message_id=10505)
    )
    row = await orch.tasks.db.fetchone("SELECT status FROM requests WHERE source_message_id=10505")
    assert row is not None and row["status"] == "retryable"


@pytest.mark.asyncio
async def test_cancellation_marks_request_cancelled(orchestrator, project) -> None:
    orch, supervisor, _workers, _notifier, _projects, _tasks = orchestrator

    async def cancel(**kwargs):
        raise asyncio.CancelledError

    supervisor.classify = cancel  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await orch.handle_incoming(
            IncomingMessage(
                chat_id=424242, user_id=424242, text="cancel", telegram_message_id=10606
            )
        )
    row = await orch.tasks.db.fetchone("SELECT status FROM requests WHERE source_message_id=10606")
    assert row is not None and row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_network_failure_is_retryable_and_owner_retry_works(orchestrator, project) -> None:
    from ai_supervisor.providers.base import ProviderConnectionError

    orch, supervisor, workers, _notifier, _projects, _tasks = orchestrator

    async def fail(**kwargs):
        raise ProviderConnectionError("network unavailable")

    supervisor.classify = fail  # type: ignore[method-assign]
    await orch.handle_incoming(
        IncomingMessage(chat_id=424242, user_id=424242, text="network", telegram_message_id=10707)
    )
    row = await orch.tasks.db.fetchone(
        "SELECT request_id, status FROM requests WHERE source_message_id=10707"
    )
    assert row is not None and row["status"] == "retryable"

    # Explicit owner retry after the transient failure; no service restart.
    supervisor.classify = type(supervisor).classify.__get__(supervisor, type(supervisor))  # type: ignore[assignment]
    await orch.retry_request(str(row["request_id"]), 424242, 424242)
    await _drain(orch)
    assert workers.calls == 1
    final = await orch.tasks.db.fetchone(
        "SELECT status FROM requests WHERE request_id=?", (row["request_id"],)
    )
    assert final is not None and final["status"] == "task"
