from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ai_supervisor.models import TokenUsage


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


class ProviderToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class ProviderResponse(BaseModel):
    content: str = ""
    reasoning_content: str = ""
    tool_calls: list[ProviderToolCall] = Field(default_factory=list)
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
