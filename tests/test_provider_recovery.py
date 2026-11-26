from __future__ import annotations

import asyncio

import pytest

from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.models import TokenUsage
from ai_supervisor.providers.base import (
    ProviderCallIdentity,
    ProviderCapabilities,
    ProviderConnectionError,
    ProviderHealth,
    ProviderResponse,
)
from ai_supervisor.providers.registry import ProviderRecoveryBusy, ProviderRegistry


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.capabilities = ProviderCapabilities()
        self.probe_calls = 0
        self.fail = False
        self.delay = 0.0

    async def probe(self, *, max_output_tokens: int = 8) -> ProviderResponse:
        self.probe_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ProviderConnectionError("network down")
        return ProviderResponse(
            content="OK",
            finish_reason="completed",
            usage=TokenUsage(input_tokens=2, output_tokens=1, requests=1),
            provider=self.name,
            model=self.model,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            available=True,
            generation_ready=None,
            checks={"connectivity": "ok", "model_list": "ok", "generation": "not_probed"},
        )

    async def close(self) -> None:
        return None


async def _registry(settings, tmp_path):
    from ai_supervisor.memory.database import Database

    db = Database(tmp_path / "recovery.db")
    await db.connect()
    registry = ProviderRegistry(settings, SecretRedactor([]), db=db)
    fake = FakeProvider()
    registry._providers.clear()
    registry._providers["fake"] = fake  # type: ignore[assignment]
    registry._roles["chat"] = "fake"
    registry._roles["planner"] = "fake"
    return db, registry, fake


def _identity(provider: str = "fake", model: str = "fake-model") -> ProviderCallIdentity:
    return ProviderCallIdentity(
        request_id="req-recovery",
        logical_call_id="call-recovery",
        attempt_id="attempt-1",
        provider_id=provider,
        model_id=model,
        role="provider_recovery",
    )


@pytest.mark.asyncio
async def test_models_list_does_not_clear_quota(settings, tmp_path) -> None:
    db, registry, _fake = await _registry(settings, tmp_path)
    try:
        registry._quota_blocked.add("fake")
        await registry.persist_state("fake", "QUOTA_EXHAUSTED", category="quota")
        await registry.health("fake")
        assert "fake" in registry.quota_blocked()
        assert (await registry.state_snapshot())["fake"]["state"] == "QUOTA_EXHAUSTED"
    finally:
        await registry.close()
        await db.close()


@pytest.mark.asyncio
async def test_probe_success_clears_persisted_block(settings, tmp_path) -> None:
    db, registry, fake = await _registry(settings, tmp_path)
    try:
        registry._quota_blocked.add("fake")
        await registry.persist_state("fake", "QUOTA_EXHAUSTED", category="quota")
        response = await registry.probe_generation("fake", _identity())
        assert response.identity is not None
        await registry.clear_provider_quota("fake")
        assert "fake" not in registry.quota_blocked()
        assert (await registry.state_snapshot())["fake"]["state"] == "AVAILABLE"
        assert fake.probe_calls == 1
        restarted = ProviderRegistry(settings, SecretRedactor([]), db=db)
        restarted._providers.clear()
        restarted._providers["fake"] = fake  # type: ignore[assignment]
        restarted._roles["chat"] = "fake"
        await restarted.hydrate()
        assert "fake" not in restarted.quota_blocked()
        assert restarted.provider_for("chat").name == "fake"
        await restarted.close()
    finally:
        await registry.close()
        await db.close()


@pytest.mark.asyncio
async def test_probe_failure_keeps_block(settings, tmp_path) -> None:
    db, registry, fake = await _registry(settings, tmp_path)
    try:
        fake.fail = True
        registry._quota_blocked.add("fake")
        with pytest.raises(ProviderConnectionError):
            await registry.probe_generation("fake", _identity())
        assert "fake" in registry.quota_blocked()
    finally:
        await registry.close()
        await db.close()


@pytest.mark.asyncio
async def test_recovery_probe_is_single_flight(settings, tmp_path) -> None:
    db, registry, fake = await _registry(settings, tmp_path)
    try:
        fake.delay = 0.2
        first = asyncio.create_task(registry.probe_generation("fake", _identity()))
        await asyncio.sleep(0.02)
        with pytest.raises(ProviderRecoveryBusy):
            await registry.probe_generation("fake", _identity())
        await first
        assert fake.probe_calls == 1
    finally:
        await registry.close()
        await db.close()


@pytest.mark.asyncio
async def test_provider_recovery_approval_flow_and_usage(settings, tmp_path) -> None:
    db, registry, fake = await _registry(settings, tmp_path)
    orchestrator = None
    try:
        from conftest import FakeNotifier, FakeSupervisor, FakeWorkers

        from ai_supervisor.core.orchestrator import Orchestrator
        from ai_supervisor.core.permissions import ApprovalService
        from ai_supervisor.core.usage import UsageService
        from ai_supervisor.memory.conversations import ConversationService
        from ai_supervisor.memory.projects import ProjectService
        from ai_supervisor.memory.tasks import TaskService

        orchestrator = Orchestrator(
            settings=settings,
            projects=ProjectService(db, settings),
            conversations=ConversationService(db, settings),
            tasks=TaskService(db),
            usage=UsageService(db, settings),
            approvals=ApprovalService(db),
            supervisor=FakeSupervisor(),
            workspace=None,  # type: ignore[arg-type]
            workers=FakeWorkers(),  # type: ignore[arg-type]
            scheduler=__import__(
                "ai_supervisor.core.worker_manager", fromlist=["WorkerScheduler"]
            ).WorkerScheduler(1),
            context_builder=None,  # type: ignore[arg-type]
            redactor=SecretRedactor([]),
            notifier=FakeNotifier(),
        )
        orchestrator.providers = registry
        registry._quota_blocked.add("fake")
        await registry.persist_state("fake", "QUOTA_EXHAUSTED", category="quota")
        await orchestrator.request_provider_recovery("fake", 424242, 424242)
        assert fake.probe_calls == 0  # approval requested, no generation yet
        pending = await orchestrator.approvals.latest_pending(user_id=424242)
        assert pending is not None
        await orchestrator.approve_action(int(pending["id"]), 424242, 424242)
        rows = await db.fetchall("SELECT provider, role, request_id FROM usage_events")
        assert rows and rows[-1]["provider"] == "fake"
        assert rows[-1]["role"] == "provider_recovery"
        assert str(rows[-1]["request_id"]).startswith("probe-")
        assert "fake" not in registry.quota_blocked()
        assert fake.probe_calls == 1
        # Replaying the same approval cannot generate again.
        await orchestrator.approve_action(int(pending["id"]), 424242, 424242)
        assert fake.probe_calls == 1
    finally:
        if orchestrator is not None:
            await orchestrator.providers.close()  # type: ignore[union-attr]
        await db.close()


@pytest.mark.asyncio
async def test_changed_provider_config_invalidates_approval(settings, tmp_path) -> None:
    from conftest import FakeNotifier, FakeSupervisor, FakeWorkers

    from ai_supervisor.core.orchestrator import Orchestrator
    from ai_supervisor.core.permissions import ApprovalService
    from ai_supervisor.core.usage import UsageService
    from ai_supervisor.memory.conversations import ConversationService
    from ai_supervisor.memory.projects import ProjectService
    from ai_supervisor.memory.tasks import TaskService

    db, registry, fake = await _registry(settings, tmp_path)
    orchestrator = Orchestrator(
        settings=settings,
        projects=ProjectService(db, settings),
        conversations=ConversationService(db, settings),
        tasks=TaskService(db),
        usage=UsageService(db, settings),
        approvals=ApprovalService(db),
        supervisor=FakeSupervisor(),
        workspace=None,  # type: ignore[arg-type]
        workers=FakeWorkers(),  # type: ignore[arg-type]
        scheduler=__import__(
            "ai_supervisor.core.worker_manager", fromlist=["WorkerScheduler"]
        ).WorkerScheduler(1),
        context_builder=None,  # type: ignore[arg-type]
        redactor=SecretRedactor([]),
        notifier=FakeNotifier(),
    )
    try:
        orchestrator.providers = registry
        registry._quota_blocked.add("fake")
        await registry.persist_state("fake", "QUOTA_EXHAUSTED", category="quota")
        await orchestrator.request_provider_recovery("fake", 424242, 424242)
        pending = await orchestrator.approvals.latest_pending(user_id=424242)
        assert pending is not None
        fake.model = "changed-model"
        await orchestrator.approve_action(int(pending["id"]), 424242, 424242)
        assert fake.probe_calls == 0
        assert "изменилась" in orchestrator.notifier.messages[-1][1]
    finally:
        await registry.close()
        await db.close()
