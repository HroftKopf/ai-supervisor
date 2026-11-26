from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.providers.kimi import KimiClient, KimiClientConfig


def _client() -> KimiClient:
    return KimiClient(
        KimiClientConfig(
            api_key="sk-test",
            base_url="https://api.kimi.ai/coding/v1",
            model="k3.1-pro",
            max_retries=0,
            max_completion_tokens=2048,
        ),
        SecretRedactor(["sk-test"]),
    )


@pytest.mark.asyncio
async def test_streaming_assembles_content_reasoning_tools_and_usage() -> None:
    client = _client()
    chunks = [
        SimpleNamespace(
            model="k3.1-pro",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        reasoning_content="think ", content=None, tool_calls=None
                    ),
                )
            ],
        ),
        SimpleNamespace(
            model="k3.1-pro",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        reasoning_content=None, content="Hello ", tool_calls=None
                    ),
                )
            ],
        ),
        SimpleNamespace(
            model="k3.1-pro",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    delta=SimpleNamespace(
                        reasoning_content=None,
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(name="submit_intent", arguments='{"a":'),
                            )
                        ],
                    ),
                )
            ],
        ),
        SimpleNamespace(
            model="k3.1-pro",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        reasoning_content=None,
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(name=None, arguments="1}"),
                            )
                        ],
                    ),
                )
            ],
        ),
        SimpleNamespace(
            model="k3.1-pro",
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                prompt_tokens_details=SimpleNamespace(cached_tokens=3),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
            choices=[],
        ),
    ]

    async def create(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["stream"] is True
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs
        assert kwargs["max_completion_tokens"] == 2048

        async def stream():
            for chunk in chunks:
                yield chunk

        return stream()

    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )
    response = await client.complete([{"role": "user", "content": "hi"}])
    assert response.content == "Hello "
    assert response.reasoning_content == "think "
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].name == "submit_intent"
    assert response.tool_calls[0].arguments == '{"a":1}'
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.usage.cached_tokens == 3
    assert response.usage.reasoning_tokens == 2
    assert response.usage.requests == 1
    await client.close()


@pytest.mark.asyncio
async def test_tool_call_assistant_message_preserves_reasoning() -> None:
    from ai_supervisor.providers.kimi import KimiResponse, KimiToolCall

    response = KimiResponse(
        content="",
        reasoning_content="reason",
        tool_calls=[KimiToolCall(id="call", name="submit_intent", arguments="{}")],
    )
    message = response.assistant_message()
    assert message["reasoning_content"] == "reason"
    assert message["tool_calls"][0]["function"]["name"] == "submit_intent"


def _fake_stream() -> SimpleNamespace:
    async def create(**kwargs):  # type: ignore[no-untyped-def]
        async def stream():
            yield SimpleNamespace(
                model="k3.1-pro",
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(
                            reasoning_content=None, content="ok", tool_calls=None
                        ),
                    )
                ],
            )

        return stream()

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_named_tool_choice_is_rejected_before_http() -> None:
    from ai_supervisor.providers.kimi import KimiConfigurationError, structured_tool

    client = _client()
    fake = _fake_stream()
    client._client = fake  # type: ignore[assignment]
    tool = structured_tool(name="submit_intent", description="x", parameters={"type": "object"})
    with pytest.raises(KimiConfigurationError, match="incompatible"):
        await client.complete(
            [{"role": "user", "content": "hi"}],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "submit_intent"}},  # type: ignore[arg-type]
        )
    await client.close()


@pytest.mark.asyncio
async def test_required_tool_choice_needs_tools() -> None:
    from ai_supervisor.providers.kimi import KimiConfigurationError

    client = _client()
    with pytest.raises(KimiConfigurationError, match="needs at least one"):
        await client.complete([{"role": "user", "content": "hi"}], tool_choice="required")
    await client.close()


@pytest.mark.asyncio
async def test_auto_and_required_are_sent_as_strings() -> None:
    from ai_supervisor.providers.kimi import structured_tool

    captured: list[dict[str, object]] = []

    async def create(**kwargs):  # type: ignore[no-untyped-def]
        captured.append(kwargs)

        async def stream():
            yield SimpleNamespace(
                model="k3.1-pro",
                usage=None,
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        delta=SimpleNamespace(
                            reasoning_content=None, content="ok", tool_calls=None
                        ),
                    )
                ],
            )

        return stream()

    client = _client()
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        close=AsyncMock(),
    )
    tool = structured_tool(name="submit_intent", description="x", parameters={"type": "object"})
    await client.complete([{"role": "user", "content": "chat"}], tool_choice="auto")
    await client.complete(
        [{"role": "user", "content": "task"}], tools=[tool], tool_choice="required"
    )
    await client.complete([{"role": "user", "content": "tools"}], tools=[tool])
    assert captured[0]["tool_choice"] == "auto"
    assert captured[1]["tool_choice"] == "required"
    assert len(captured[1]["tools"]) == 1  # type: ignore[arg-type]
    assert captured[2]["tool_choice"] == "auto"
    assert all(not isinstance(item["tool_choice"], dict) for item in captured)
    await client.close()


def test_parse_tool_call_rejects_multiple_calls() -> None:
    from ai_supervisor.providers.kimi import (
        KimiProtocolError,
        KimiResponse,
        KimiToolCall,
        parse_tool_call,
    )

    response = KimiResponse(
        tool_calls=[
            KimiToolCall(id="1", name="submit_intent", arguments="{}"),
            KimiToolCall(id="2", name="other", arguments="{}"),
        ]
    )
    with pytest.raises(KimiProtocolError, match="несколько"):
        parse_tool_call(response, "submit_intent")


@pytest.mark.asyncio
async def test_quota_error_code_and_retry_after_are_classified() -> None:
    from ai_supervisor.providers.kimi import (
        KimiConnectionError,
        KimiQuotaError,
        KimiRateLimitError,
    )

    client = _client()
    quota = SimpleNamespace(
        status_code=429,
        body={"error": {"code": "exceeded_current_quota_error", "message": "quota"}},
        response=SimpleNamespace(headers={}),
    )
    assert isinstance(client._map_status_error(quota), KimiQuotaError)

    limited = SimpleNamespace(
        status_code=429,
        body={"error": {"code": "rate_limit_exceeded", "message": "slow down"}},
        response=SimpleNamespace(headers={"retry-after": "7"}),
    )
    error = client._map_status_error(limited)
    assert isinstance(error, KimiRateLimitError)
    assert error.retry_after == 7

    unavailable = SimpleNamespace(
        status_code=503,
        body={"error": {"message": "overloaded"}},
        response=SimpleNamespace(headers={}),
    )
    assert isinstance(client._map_status_error(unavailable), KimiConnectionError)
    await client.close()
