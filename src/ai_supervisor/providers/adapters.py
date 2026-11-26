from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.models import TokenUsage
from ai_supervisor.providers.base import (
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderConnectionError,
    ProviderError,
    ProviderHealth,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderToolCall,
)
from ai_supervisor.providers.kimi import (
    KimiClient,
    KimiClientConfig,
    KimiError,
    KimiResponse,
)


def _convert(response: KimiResponse, provider: str) -> ProviderResponse:
    return ProviderResponse(
        content=response.content,
        reasoning_content=response.reasoning_content,
        tool_calls=[
            ProviderToolCall(id=call.id, name=call.name, arguments=call.arguments)
            for call in response.tool_calls
        ],
        finish_reason=response.finish_reason,
        usage=response.usage,
        model=response.model,
        request_id=response.request_id,
        provider=provider,
        raw_usage=(
            response.raw_usage if hasattr(response, "raw_usage") else response.usage.model_dump()
        ),
    )


def normalize_error(exc: Exception, provider: str) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, KimiError):
        name = type(exc).__name__
        mapping = {
            "KimiAuthenticationError": ProviderAuthenticationError,
            "KimiPermissionError": ProviderPermissionError,
            "KimiQuotaError": ProviderQuotaError,
            "KimiRateLimitError": ProviderRateLimitError,
            "KimiConnectionError": ProviderConnectionError,
            "KimiProtocolError": ProviderProtocolError,
        }
        error = mapping.get(name, ProviderError)(f"{provider}: {exc}")
        error.provider = provider
        return error
    error = ProviderError(f"{provider}: {type(exc).__name__}: {exc}")
    error.provider = provider
    return error


class KimiProvider:
    name = "kimi"

    def __init__(self, client: KimiClient, redactor: SecretRedactor | None = None) -> None:
        self.client = client
        self.model = client.config.model
        self.capabilities = ProviderCapabilities(
            tools=True,
            structured_output=True,
            streaming=True,
            reasoning=True,
            context_window=262_144,
            max_output_tokens=client.config.max_completion_tokens,
            prompt_cache=True,
        )

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
    ) -> ProviderResponse:
        try:
            response = await self.client.complete(
                messages,
                max_completion_tokens=max_completion_tokens,
                tools=tools,
                tool_choice=tool_choice,
                prompt_cache_key=prompt_cache_key,
                purpose=purpose,
                reasoning_effort=reasoning_effort,
            )
            return _convert(response, self.name)
        except Exception as exc:
            raise normalize_error(exc, self.name) from exc

    async def health(self) -> ProviderHealth:
        try:
            models = await self.client.validate_model()
            return ProviderHealth(
                available=True,
                detail="Kimi API/models reachable; generation quota not probed",
                models=models,
                generation_ready=None,
                checks={"connectivity": "ok", "model_list": "ok", "generation": "not_probed"},
            )
        except Exception as exc:
            return ProviderHealth(
                available=False, detail=str(normalize_error(exc, self.name))[:500]
            )

    async def close(self) -> None:
        await self.client.close()


class OpenAICompatibleProvider:
    """Chat Completions-compatible provider for third-party/local endpoints."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: str,
        model: str,
        redactor: SecretRedactor | None = None,
        capabilities: ProviderCapabilities | None = None,
        max_output_tokens: int = 8192,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.name = name
        self.model = model
        self.capabilities = capabilities or ProviderCapabilities()
        self._client = KimiClient(
            KimiClientConfig(
                api_key=api_key,
                base_url=base_url,
                model=model,
                reasoning_effort="",
                max_completion_tokens=max_output_tokens,
                request_timeout_seconds=timeout_seconds,
                max_retries=1,
                max_concurrent_requests=1,
            ),
            redactor,
        )

    async def complete(
        self, messages: Sequence[Mapping[str, Any]], **kwargs: Any
    ) -> ProviderResponse:
        if kwargs.get("reasoning_effort") and not self.capabilities.reasoning:
            kwargs["reasoning_effort"] = None
        try:
            response = await self._client.complete(messages, **kwargs)
            return _convert(response, self.name)
        except Exception as exc:
            raise normalize_error(exc, self.name) from exc

    async def health(self) -> ProviderHealth:
        try:
            models = await self._client.validate_model()
            return ProviderHealth(
                available=True,
                models=models,
                generation_ready=None,
                checks={"connectivity": "ok", "model_list": "ok", "generation": "not_probed"},
            )
        except Exception as exc:
            return ProviderHealth(
                available=False, detail=str(normalize_error(exc, self.name))[:500]
            )

    async def close(self) -> None:
        await self._client.close()


class OpenAIProvider:
    """OpenAI Responses API adapter.

    Documented request shape: instructions, input, tools (flat function tools),
    max_output_tokens, reasoning, text/format; outputs are output items.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4.1",
        reasoning: bool = True,
        prompt_cache: bool = True,
        timeout_seconds: float = 180.0,
        client: Any | None = None,
    ) -> None:
        import openai

        self.model = model
        self.capabilities = ProviderCapabilities(
            tools=True,
            structured_output=True,
            streaming=True,
            reasoning=reasoning,
            context_window=128_000,
            max_output_tokens=32_768,
            prompt_cache=prompt_cache,
        )
        if client is not None:
            self._client = client
        else:
            self._client = openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url.rstrip("/"),
                timeout=timeout_seconds,
                max_retries=0,
            )

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
    ) -> ProviderResponse:
        system_parts: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = message.get("content")
            if role == "system":
                system_parts.append(str(content or ""))
            elif role in {"user", "assistant"}:
                if role == "assistant" and message.get("tool_calls"):
                    for call in message.get("tool_calls") or []:
                        function = call.get("function") if isinstance(call, Mapping) else None
                        if isinstance(function, Mapping):
                            input_items.append(
                                {
                                    "type": "function_call",
                                    "call_id": str(call.get("id") or ""),
                                    "name": str(function.get("name") or ""),
                                    "arguments": str(function.get("arguments") or "{}"),
                                }
                            )
                    if content:
                        input_items.append({"role": role, "content": self._content(content)})
                else:
                    input_items.append({"role": role, "content": self._content(content)})
            elif role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(message.get("tool_call_id") or ""),
                        "output": str(content or ""),
                    }
                )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "max_output_tokens": max_completion_tokens or self.capabilities.max_output_tokens,
        }
        if system_parts:
            kwargs["instructions"] = "\n\n".join(system_parts)
        if tools:
            kwargs["tools"] = [{"type": "function", **self._function_tool(tool)} for tool in tools]
        if tool_choice in {"auto", "none", "required"}:
            kwargs["tool_choice"] = tool_choice
        if reasoning_effort and self.capabilities.reasoning:
            # OpenAI reasoning accepts low/medium/high; map the internal "max" profile.
            effort = "high" if reasoning_effort == "max" else reasoning_effort
            kwargs["reasoning"] = {"effort": effort}
        if prompt_cache_key and self.capabilities.prompt_cache:
            kwargs["prompt_cache_key"] = prompt_cache_key
        if stream:
            kwargs["stream"] = True
        try:
            response = await self._client.responses.create(**kwargs)
            if stream:
                return await self._consume_stream(response)
            return self._normalize(response)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._normalize_error(exc) from exc

    async def _consume_stream(self, stream: Any) -> ProviderResponse:
        text_parts: list[str] = []
        calls: dict[str, dict[str, str]] = {}
        final: Any | None = None
        async for event in stream:
            event_type = str(getattr(event, "type", "") or "")
            if event_type == "response.output_text.delta":
                text_parts.append(str(getattr(event, "delta", "") or ""))
            elif event_type in {"response.output_item.added", "response.output_item.done"}:
                item = getattr(event, "item", None)
                if item is not None and getattr(item, "type", "") == "function_call":
                    key = str(getattr(item, "id", None) or getattr(item, "call_id", "") or "call")
                    calls[key] = {
                        "id": str(getattr(item, "call_id", None) or key),
                        "name": str(getattr(item, "name", "") or ""),
                        "arguments": str(getattr(item, "arguments", "") or ""),
                    }
            elif event_type == "response.function_call_arguments.delta":
                key = str(getattr(event, "item_id", "") or "")
                entry = calls.setdefault(key, {"id": key or "call", "name": "", "arguments": ""})
                entry["arguments"] += str(getattr(event, "delta", "") or "")
            elif event_type == "response.completed":
                final = getattr(event, "response", None)
        if final is not None:
            normalized = self._normalize(final)
            if text_parts and not normalized.content:
                normalized.content = "".join(text_parts)
            return normalized
        return ProviderResponse(
            content="".join(text_parts),
            tool_calls=[
                ProviderToolCall(
                    id=item["id"], name=item["name"], arguments=item["arguments"] or "{}"
                )
                for item in calls.values()
                if item["name"]
            ],
            finish_reason="completed",
            model=self.model,
            provider=self.name,
        )

    @staticmethod
    def _content(content: Any) -> Any:
        if not isinstance(content, list):
            return content
        converted: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, Mapping):
                converted.append({"type": "input_text", "text": str(part)})
            elif part.get("type") == "text":
                converted.append({"type": "input_text", "text": str(part.get("text") or "")})
            elif part.get("type") == "image_url":
                image = part.get("image_url") or {}
                converted.append(
                    {
                        "type": "input_image",
                        "image_url": image.get("url") if isinstance(image, Mapping) else image,
                    }
                )
            else:
                converted.append(dict(part))
        return converted

    @staticmethod
    def _function_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
        function = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
        return {
            "name": str(function.get("name") or ""),
            "description": str(function.get("description") or ""),
            "parameters": dict(function.get("parameters") or {}),
            "strict": bool(function.get("strict", True)),
        }

    def _normalize(self, response: Any) -> ProviderResponse:
        text_parts: list[str] = []
        calls: list[ProviderToolCall] = []
        for item in getattr(response, "output", None) or []:
            item_type = getattr(item, "type", "")
            if item_type == "function_call":
                calls.append(
                    ProviderToolCall(
                        id=str(getattr(item, "call_id", None) or getattr(item, "id", "") or "call"),
                        name=str(getattr(item, "name", "") or ""),
                        arguments=str(getattr(item, "arguments", "") or "{}"),
                    )
                )
            elif item_type == "message":
                for part in getattr(item, "content", None) or []:
                    text = getattr(part, "text", None)
                    if text:
                        text_parts.append(str(text))
        if not text_parts:
            output_text = getattr(response, "output_text", None)
            if output_text:
                text_parts.append(str(output_text))
        usage = TokenUsage()
        raw_usage: dict[str, Any] = {}
        usage_obj = getattr(response, "usage", None)
        if usage_obj is not None:
            raw_usage = {
                "input_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
                "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
            }
            input_details = getattr(usage_obj, "input_tokens_details", None)
            output_details = getattr(usage_obj, "output_tokens_details", None)
            cached = getattr(input_details, "cached_tokens", 0) if input_details else 0
            reasoning_tokens = (
                getattr(output_details, "reasoning_tokens", 0) if output_details else 0
            )
            usage = TokenUsage(
                input_tokens=int(raw_usage["input_tokens"]),
                output_tokens=int(raw_usage["output_tokens"]),
                cached_tokens=int(cached or 0),
                reasoning_tokens=int(reasoning_tokens or 0),
                requests=1,
            )
        status = str(getattr(response, "status", "") or "")
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) if details else None
        finish_reason = (
            "length" if status == "incomplete" and reason == "max_output_tokens" else status or None
        )
        return ProviderResponse(
            content="".join(text_parts),
            tool_calls=calls,
            finish_reason=finish_reason,
            usage=usage,
            model=str(getattr(response, "model", None) or self.model),
            request_id=str(getattr(response, "id", "") or "") or None,
            provider=self.name,
            raw_usage=raw_usage,
        )

    def _normalize_error(self, exc: Exception) -> ProviderError:
        name = type(exc).__name__
        status = getattr(exc, "status_code", None)
        mapping: dict[int, type[ProviderError]] = {
            401: ProviderAuthenticationError,
            403: ProviderPermissionError,
            429: ProviderRateLimitError,
        }
        klass = mapping.get(status) if isinstance(status, int) else None
        if klass is None:
            if "RateLimit" in name or "RateLimit" in str(exc):
                klass = ProviderRateLimitError
            elif "Authentication" in name:
                klass = ProviderAuthenticationError
            elif "Connection" in name or "Timeout" in name:
                klass = ProviderConnectionError
            else:
                klass = ProviderError
        error = klass(f"openai: {name}: {str(exc)[:500]}")
        error.provider = self.name
        return error

    async def health(self) -> ProviderHealth:
        try:
            models = await self._client.models.list()
            names = [str(getattr(item, "id", "")) for item in getattr(models, "data", [])]
            return ProviderHealth(
                available=True,
                models=names,
                generation_ready=None,
                checks={"connectivity": "ok", "model_list": "ok", "generation": "not_probed"},
            )
        except Exception as exc:
            return ProviderHealth(available=False, detail=str(self._normalize_error(exc))[:500])

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
