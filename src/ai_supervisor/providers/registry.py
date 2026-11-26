from __future__ import annotations

import dataclasses
from typing import Any

from ai_supervisor.config import Settings
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.providers.adapters import (
    KimiProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)
from ai_supervisor.providers.base import (
    LLMProvider,
    ProviderCapabilities,
    ProviderConfigurationError,
)
from ai_supervisor.providers.kimi import KimiClient, KimiClientConfig

ROLES = ("chat", "classifier", "planner", "reviewer", "summarizer", "experience")


class ProviderRegistry:
    """Explicit role -> provider routing. Never falls back to another paid API silently."""

    def __init__(self, settings: Settings, redactor: SecretRedactor) -> None:
        self.settings = settings
        self.redactor = redactor
        self._providers: dict[str, LLMProvider] = {}
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
        if role not in ROLES:
            raise ProviderConfigurationError(f"unknown LLM role: {role}")
        name = self._roles[role]
        provider = self._providers.get(name)
        if provider is None:
            raise ProviderConfigurationError(
                f"role {role} is configured for provider {name}, but its credentials are missing; "
                "no fallback to another paid API is performed"
            )
        return provider

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
            "providers": {
                name: {
                    "model": provider.model,
                    "capabilities": dataclasses.asdict(provider.capabilities),
                }
                for name, provider in self._providers.items()
            },
        }

    def select(self, role: str, provider_name: str) -> None:
        if role not in ROLES:
            raise ProviderConfigurationError(f"unknown LLM role: {role}")
        if provider_name not in self._providers:
            raise ProviderConfigurationError(
                f"provider {provider_name!r} is not available/configured"
            )
        self._roles[role] = provider_name

    async def health(self, role_or_provider: str = "") -> dict[str, Any]:
        target = role_or_provider
        if target in ROLES:
            provider = self.provider_for(target)
            result = await provider.health()
            return {target: result.model_dump()}
        if target:
            providers = {target: self._providers[target]} if target in self._providers else {}
        else:
            providers = self._providers
        return {
            name: (await provider.health()).model_dump() for name, provider in providers.items()
        }

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()
