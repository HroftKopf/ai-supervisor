from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ai_supervisor.models import TokenUsage


class AgentRole(StrEnum):
    CHAT = "chat"
    CLASSIFIER = "classifier"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    SUMMARIZER = "summarizer"
    EXPERIENCE = "experience"
    ANALYSIS = "analysis"


ROLE_ALIASES = {
    "planning": AgentRole.PLANNER.value,
    "classification": AgentRole.CLASSIFIER.value,
    "review": AgentRole.REVIEWER.value,
}


def canonical_role(role: str) -> str:
    value = str(role).strip().lower()
    return ROLE_ALIASES.get(value, value)


class ProviderError(RuntimeError):
    """Normalized provider failure; contains no credentials."""


class ProviderConfigurationError(ProviderError):
    pass


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderPermissionError(ProviderError):
    pass


class ProviderQuotaError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderConnectionError(ProviderError):
    pass


class ProviderProtocolError(ProviderError):
    pass


class ProviderCapabilityError(ProviderError):
    pass


class ProviderCallIdentity(BaseModel):
    request_id: str
    logical_call_id: str
    attempt_id: str
    provider_id: str
    account_scope_id: str = "default"
    model_id: str
    role: str
    project_id: int | None = None
    task_id: int | None = None
    stage_id: int | None = None
    prompt_version: str = ""
    reasoning_profile: str = ""


class ProviderToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class ProviderResponse(BaseModel):
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ProviderToolCall] = Field(default_factory=list)
    identity: ProviderCallIdentity | None = None
    finish_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""
    request_id: str | None = None
    provider: str = ""
    raw_usage: dict[str, Any] = Field(default_factory=dict)

    def assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class ProviderHealth(BaseModel):
    available: bool
    detail: str = ""
    models: list[str] = Field(default_factory=list)
    # Reachability/model listing is not proof that paid generation is available.
    generation_ready: bool | None = None
    checks: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    tools: bool = True
    structured_output: bool = True
    streaming: bool = True
    reasoning: bool = True
    context_window: int = 128_000
    max_output_tokens: int = 16_384
    prompt_cache: bool = False
    native_tool_calls: bool = True


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str
    capabilities: ProviderCapabilities

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_completion_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Literal["auto", "none", "required"] | None = None,
        prompt_cache_key: str | None = None,
        purpose: str = "chat",
        reasoning_effort: str | None = None,
        stream: bool = False,
        identity: ProviderCallIdentity | None = None,
    ) -> ProviderResponse: ...

    async def health(self) -> ProviderHealth: ...

    async def close(self) -> None: ...


def function_schema(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the internal JSON-schema function tool for any provider."""
    if tool.get("type") == "function" and isinstance(tool.get("function"), Mapping):
        function = dict(tool["function"])  # type: ignore[index]
        return {
            "name": str(function.get("name") or ""),
            "description": str(function.get("description") or ""),
            "parameters": dict(function.get("parameters") or {}),
            "strict": bool(function.get("strict", True)),
        }
    return dict(tool)
