from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from ai_supervisor.config import Settings
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.memory.database import Database
from ai_supervisor.providers.adapters import (
    KimiProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from ai_supervisor.providers.base import (
    AgentRole,
    LLMProvider,
    ProviderCallIdentity,
    ProviderCapabilities,
    ProviderConfigurationError,
    ProviderQuotaError,
    canonical_role,
)
from ai_supervisor.providers.kimi import KimiClient, KimiClientConfig
from ai_supervisor.providers.state import ProviderStateStore

ROLES = tuple(role.value for role in AgentRole)


class ProviderRecoveryError(ProviderQuotaError):
    pass


class ProviderRecoveryBusy(ProviderRecoveryError):
    pass


class ProviderRecoveryStateError(ProviderRecoveryError):
    pass


class ProviderRecoveryPersistenceError(ProviderRecoveryError):
    pass


class ProviderRegistry:
    """Explicit role -> provider routing. Never falls back to another paid API silently."""

    def __init__(
        self, settings: Settings, redactor: SecretRedactor, db: Database | None = None
    ) -> None:
        self.settings = settings
        self.redactor = redactor
        self.state_store = ProviderStateStore(db) if db is not None else None
        self._providers: dict[str, LLMProvider] = {}
        self._quota_blocked: set[str] = set()
        self._recovery_locks: dict[str, asyncio.Lock] = {}
        self._roles: dict[str, str] = {
            role: settings.llm.roles.get(role, settings.llm.default_provider) for role in ROLES
        }
        self._build_available()

    def _build_available(self) -> None:
        s = self.settings
        if s.kimi_key:
            self._providers["kimi"] = KimiProvider(
                KimiClient(
                    KimiClientConfig(
                        api_key=s.kimi_key,
                        base_url=s.kimi.base_url,
                        model=s.kimi.model,
                        reasoning_effort=s.kimi.reasoning_effort,
                        max_completion_tokens=s.kimi.max_completion_tokens,
                        request_timeout_seconds=s.kimi.request_timeout_seconds,
                        max_retries=s.kimi.max_retries,
                        max_concurrent_requests=s.kimi.max_concurrent_requests,
                    ),
                    self.redactor,
                ),
                self.redactor,
            )
        if s.openai_key:
            self._providers["openai"] = OpenAIProvider(
                api_key=s.openai_key,
                base_url=s.llm.openai_base_url,
                model=s.llm.openai_model,
                reasoning=s.llm.openai_reasoning,
                prompt_cache=s.llm.openai_prompt_cache,
            )
        if s.openai_compatible_key and s.llm.compatible_base_url and s.llm.compatible_model:
            self._providers["openai_compatible"] = OpenAICompatibleProvider(
                name="openai_compatible",
                api_key=s.openai_compatible_key,
                base_url=s.llm.compatible_base_url,
                model=s.llm.compatible_model,
                redactor=self.redactor,
                capabilities=ProviderCapabilities(
                    tools=True,
                    structured_output=True,
                    streaming=True,
                    reasoning=s.llm.compatible_reasoning,
                    prompt_cache=s.llm.compatible_prompt_cache,
                ),
            )

    @property
    def roles(self) -> dict[str, str]:
        return dict(self._roles)

    def available(self) -> dict[str, LLMProvider]:
        return dict(self._providers)

    def provider_for(self, role: str) -> LLMProvider:
        role = canonical_role(role)
        if role not in ROLES:
            raise ProviderConfigurationError(f"unknown LLM role: {role}")
        name = self._roles[role]
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderConfigurationError(
                f"role {role} is configured for provider {name}, but its credentials are missing; "
                "no fallback to another paid API is performed"
            )
        if name in self._quota_blocked:
            error = ProviderQuotaError(
                f"provider {name} is paused after quota exhaustion; run /provider recover {name}"
            )
            error.provider = name
            raise error
        return provider

    def resolve_target(self, target: str) -> tuple[str, LLMProvider]:
        canonical = canonical_role(target)
        if canonical in ROLES:
            name = self._roles[canonical]
            provider = self._providers.get(name)
            if provider is None:
                raise ProviderConfigurationError(
                    f"role {canonical} is configured for unavailable provider {name}"
                )
            return name, provider
        provider = self._providers.get(target)
        if provider is None:
            raise ProviderConfigurationError(f"provider {target!r} is not available/configured")
        return target, provider

    async def recover_generation(
        self,
        provider_name: str,
        identity: ProviderCallIdentity,
        *,
        expected_generation: int,
    ) -> Any:
        """Atomically validate, probe and persist recovery under one lock.

        The lock is held across the probe and the SQLite write. The in-memory
        block is cleared only after the persisted AVAILABLE transition succeeds.
        """
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderConfigurationError(f"provider {provider_name!r} is not configured")
        lock = self._recovery_locks.setdefault(provider_name, asyncio.Lock())
        if lock.locked():
            error = ProviderRecoveryBusy(f"recovery probe for {provider_name} is already running")
            error.provider = provider_name
            raise error
        async with lock:
            if self.state_store is not None:
                current = await self.state_store.get(provider_name)
                if current is None:
                    raise ProviderRecoveryStateError("provider state is missing")
                if str(current.get("state")) != "QUOTA_EXHAUSTED":
                    raise ProviderRecoveryStateError("provider is already no longer blocked")
                if int(current.get("generation") or 0) != int(expected_generation):
                    raise ProviderRecoveryStateError(
                        "approval belongs to an older quota state generation"
                    )
            try:
                response = await provider.probe(max_output_tokens=8)
            except Exception as exc:
                if self.state_store is not None:
                    await self.state_store.set_state(
                        provider_name,
                        "QUOTA_EXHAUSTED",
                        category=type(exc).__name__,
                        reason=str(exc)[:500],
                    )
                exc.provider = provider_name  # type: ignore[attr-defined]
                raise
            if self.state_store is not None:
                try:
                    await self.state_store.set_available(provider_name)
                except Exception as exc:
                    # Keep both persisted and in-memory block on write failure.
                    raise ProviderRecoveryPersistenceError(
                        "failed to persist provider recovery; block retained"
                    ) from exc
            self._quota_blocked.discard(provider_name)
            return response.model_copy(update={"identity": identity})

    async def probe_generation(self, provider_name: str, identity: ProviderCallIdentity) -> Any:
        """Single-flight, no-tool, no-history minimal generation probe.

        Deliberately bypasses the quota gate; it is reachable only through the
        owner-approved recovery path.
        """
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderConfigurationError(f"provider {provider_name!r} is not configured")
        lock = self._recovery_locks.setdefault(provider_name, asyncio.Lock())
        if lock.locked():
            raise ProviderRecoveryBusy(f"recovery probe for {provider_name} is already running")
        async with lock:
            response = await provider.probe(max_output_tokens=8)
            return response.model_copy(update={"identity": identity})

    async def clear_provider_quota(self, provider_name: str) -> None:
        if self.state_store is not None:
            await self.state_store.set_available(provider_name)
        self.clear_quota(provider_name)

    def mark_quota_exhausted(self, role: str) -> None:
        role = canonical_role(role)
        name = self._roles.get(role)
        if name:
            self._quota_blocked.add(name)

    def mark_provider_quota(self, provider_name: str) -> None:
        if provider_name in self._providers:
            self._quota_blocked.add(provider_name)

    async def persist_state(
        self, provider_name: str, state: str, *, category: str = "", reason: str = ""
    ) -> None:
        if self.state_store is not None:
            await self.state_store.set_state(
                provider_name, state=state, category=category, reason=reason
            )

    async def hydrate(self) -> None:
        if self.state_store is None:
            return
        states = await self.state_store.list()
        blocked = {
            name
            for name, data in states.items()
            if str(data.get("state")) in {"QUOTA_EXHAUSTED", "AUTH_FAILED", "DISABLED"}
        }
        self._quota_blocked.update(blocked & set(self._providers))

    async def state_snapshot(self) -> dict[str, dict[str, Any]]:
        return await self.state_store.list() if self.state_store is not None else {}

    def clear_quota(self, provider_name: str) -> None:
        self._quota_blocked.discard(provider_name)

    def quota_blocked(self) -> set[str]:
        return set(self._quota_blocked)

    def model_for(self, role: str) -> str:
        return self.provider_for(role).model

    def snapshot(self) -> dict[str, Any]:
        return {
            "roles": {
                role: {
                    "provider": self._roles[role],
                    "available": self._roles[role] in self._providers,
                    "model": (
                        self._providers[self._roles[role]].model
                        if self._roles[role] in self._providers
                        else None
                    ),
                    "capabilities": (
                        dataclasses.asdict(self._providers[self._roles[role]].capabilities)
                        if self._roles[role] in self._providers
                        else None
                    ),
                }
                for role in ROLES
            },
            "quota_blocked": sorted(self._quota_blocked),
            "providers": {
                name: {
                    "model": provider.model,
                    "capabilities": dataclasses.asdict(provider.capabilities),
                }
                for name, provider in self._providers.items()
            },
        }

    def select(self, role: str, provider_name: str) -> None:
        role = canonical_role(role)
        if role not in ROLES:
            raise ProviderConfigurationError(f"unknown LLM role: {role}")
        if provider_name not in self._providers:
            raise ProviderConfigurationError(
                f"provider {provider_name!r} is not available/configured"
            )
        self._roles[role] = provider_name

    async def health(self, role_or_provider: str = "") -> dict[str, Any]:
        target = (
            canonical_role(role_or_provider)
            if role_or_provider in ROLES
            or role_or_provider in {"planning", "classification", "review"}
            else role_or_provider
        )
        if target in ROLES:
            name = self._roles[target]
            provider = self._providers.get(name)
            if provider is None:
                raise ProviderConfigurationError(
                    f"role {target} is configured for unavailable provider {name}"
                )
            result = await provider.health()
            if result.generation_ready is True:
                self.clear_quota(provider.name)
                if self.state_store is not None:
                    await self.state_store.set_available(provider.name)
            return {target: result.model_dump()}
        if target:
            providers = {target: self._providers[target]} if target in self._providers else {}
        else:
            providers = self._providers
        results: dict[str, Any] = {}
        for name, provider in providers.items():
            result = await provider.health()
            if result.generation_ready is True:
                self.clear_quota(name)
                if self.state_store is not None:
                    await self.state_store.set_available(name)
            results[name] = result.model_dump()
        return results

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
