from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai_supervisor.providers.adapters import KimiProvider, OpenAIProvider
from ai_supervisor.providers.base import (
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderToolCall,
)
from ai_supervisor.providers.kimi import KimiResponse, KimiToolCall
from ai_supervisor.providers.registry import ProviderRegistry


def _tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "submit_intent",
            "description": "classify",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "strict": True,
        },
    }


class FakeResponses:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None) -> None:
        self.responses = FakeResponses(response, error)
        self.models = SimpleNamespace(list=self._models)

    async def _models(self):
        return SimpleNamespace(data=[SimpleNamespace(id="gpt-test")])


def _response() -> SimpleNamespace:
    return SimpleNamespace(
        id="resp_1",
        model="gpt-test",
        status="completed",
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="hello")],
            ),
            SimpleNamespace(
                type="function_call",
                call_id="call_1",
                name="submit_intent",
                arguments=json.dumps({"kind": "chat"}),
            ),
        ],
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            input_tokens_details=SimpleNamespace(cached_tokens=3),
            output_tokens_details=SimpleNamespace(reasoning_tokens=2),
        ),
    )


@pytest.mark.asyncio
async def test_openai_responses_tool_call_and_usage() -> None:
    client = FakeClient(_response())
    provider = OpenAIProvider(api_key="secret", client=client)  # type: ignore[arg-type]
    result = await provider.complete(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ],
        tools=[_tool()],
        tool_choice="required",
        max_completion_tokens=123,
        reasoning_effort="max",
        prompt_cache_key="conv-1",
    )
    assert result.tool_calls == [
        ProviderToolCall(id="call_1", name="submit_intent", arguments='{"kind": "chat"}')
    ]
    assert result.content == "hello"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.usage.cached_tokens == 3
    assert result.usage.reasoning_tokens == 2
    assert result.usage.total_tokens == 15
    call = client.responses.calls[0]
    assert call["instructions"] == "system"
    assert call["input"] == [{"role": "user", "content": "hello"}]
    assert call["max_output_tokens"] == 123
    assert call["tools"][0]["name"] == "submit_intent"
    assert call["reasoning"] == {"effort": "high"}
    assert call["prompt_cache_key"] == "conv-1"


@pytest.mark.asyncio
async def test_openai_capability_negotiation_omits_reasoning() -> None:
    client = FakeClient(_response())
    provider = OpenAIProvider(api_key="secret", client=client, reasoning=False)  # type: ignore[arg-type]
    await provider.complete(
        [{"role": "user", "content": "hello"}],
        reasoning_effort="max",
    )
    assert "reasoning" not in client.responses.calls[0]


@pytest.mark.asyncio
async def test_openai_normalizes_errors() -> None:
    error = Exception("rate limited")
    error.status_code = 429  # type: ignore[attr-defined]
    provider = OpenAIProvider(api_key="secret", client=FakeClient(error=error))  # type: ignore[arg-type]
    with pytest.raises(ProviderRateLimitError):
        await provider.complete([{"role": "user", "content": "hello"}])

    auth = Exception("bad key")
    auth.status_code = 401  # type: ignore[attr-defined]
    provider = OpenAIProvider(api_key="secret", client=FakeClient(error=auth))  # type: ignore[arg-type]
    with pytest.raises(ProviderAuthenticationError):
        await provider.complete([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_kimi_provider_preserves_existing_normalized_contract() -> None:
    class FakeKimi:
        config = SimpleNamespace(model="k3.1-pro", max_completion_tokens=4096)

        async def complete(self, messages, **kwargs):
            return KimiResponse(
                content="ok",
                tool_calls=[KimiToolCall(id="c", name="x", arguments="{}")],
                usage=__import__("ai_supervisor.models", fromlist=["TokenUsage"]).TokenUsage(
                    input_tokens=1, output_tokens=2, requests=1
                ),
            )

        async def close(self):
            return None

    provider = KimiProvider(FakeKimi())  # type: ignore[arg-type]
    result = await provider.complete([{"role": "user", "content": "x"}])
    assert isinstance(result, ProviderResponse)
    assert result.provider == "kimi"
    assert result.tool_calls[0].name == "x"
    assert result.usage.total_tokens == 3


@pytest.mark.asyncio
async def test_registry_requires_explicit_role_provider_without_fallback(settings) -> None:
    settings = settings.model_copy(
        update={
            "llm": settings.llm.model_copy(
                update={
                    "default_provider": "kimi",
                    "roles": {"planner": "openai", "chat": "kimi"},
                }
            )
        }
    )
    # No openai key in the fixture: role must fail explicitly, not fall back to Kimi.
    registry = ProviderRegistry(
        settings,
        __import__("ai_supervisor.core.redaction", fromlist=["SecretRedactor"]).SecretRedactor([]),
    )
    try:
        with pytest.raises(Exception, match=r"credentials are missing|OPENAI"):
            registry.provider_for("planner")
    finally:
        await registry.close()


class FakeStream:
    def __init__(self) -> None:
        self.items = [
            SimpleNamespace(
                type="response.output_item.added",
                item=SimpleNamespace(
                    type="function_call",
                    id="item_1",
                    call_id="call_1",
                    name="submit_intent",
                    arguments="",
                ),
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta", item_id="item_1", delta='{"kind":'
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta", item_id="item_1", delta='"chat"}'
            ),
            SimpleNamespace(type="response.output_text.delta", delta="streamed"),
            SimpleNamespace(type="response.completed", response=_response()),
        ]

    def __aiter__(self):
        self._iter = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


@pytest.mark.asyncio
async def test_openai_streaming_accumulates_text_and_tool_arguments() -> None:
    class StreamClient:
        def __init__(self) -> None:
            self.responses = SimpleNamespace(create=self._create)
            self.models = SimpleNamespace(list=self._models)

        async def _create(self, **kwargs):
            assert kwargs["stream"] is True
            return FakeStream()

        async def _models(self):
            return SimpleNamespace(data=[])

    provider = OpenAIProvider(api_key="secret", client=StreamClient())  # type: ignore[arg-type]
    result = await provider.complete([{"role": "user", "content": "x"}], stream=True)
    assert result.content == "hello"
    assert result.tool_calls[0].name == "submit_intent"
    assert result.tool_calls[0].arguments == '{"kind": "chat"}'
    assert result.usage.requests == 1


@pytest.mark.asyncio
async def test_registry_independent_role_provider_selection(settings) -> None:
    from pydantic import SecretStr

    llm = settings.llm.model_copy(
        update={
            "default_provider": "kimi",
            "roles": {"chat": "openai", "planner": "kimi", "reviewer": "openai"},
        }
    )
    secrets = settings.secrets.model_copy(
        update={
            "kimi_api_key": SecretStr("kimi-secret"),
            "openai_api_key": SecretStr("openai-secret"),
        }
    )
    settings = settings.model_copy(update={"llm": llm, "secrets": secrets})
    registry = ProviderRegistry(
        settings,
        __import__("ai_supervisor.core.redaction", fromlist=["SecretRedactor"]).SecretRedactor([]),
    )
    try:
        snapshot = registry.snapshot()
        assert snapshot["roles"]["chat"]["capabilities"]["tools"] is True
        assert "openai-secret" not in str(snapshot)
        assert "kimi-secret" not in str(snapshot)
        assert registry.provider_for("chat").name == "openai"
        assert registry.provider_for("planner").name == "kimi"
        assert registry.provider_for("planning").name == "kimi"
        assert registry.provider_for("reviewer").model == settings.llm.openai_model
        registry.select("summarizer", "openai")
        assert registry.provider_for("summarizer").name == "openai"
        # A provider-scoped quota never blocks a different provider/role.
        registry.mark_provider_quota("kimi")
        with pytest.raises(Exception, match="paused after quota"):
            registry.provider_for("planner")
        assert registry.provider_for("reviewer").name == "openai"
        registry.mark_quota_exhausted("chat")
        with pytest.raises(Exception, match="paused after quota"):
            registry.provider_for("chat")
        assert "openai" in registry.snapshot()["quota_blocked"]
    finally:
        await registry.close()
