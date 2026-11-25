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
