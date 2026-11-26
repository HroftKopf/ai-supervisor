from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from ai_supervisor.agents.supervisor import SupervisorAgent, SupervisorError
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.models import IntentKind, TokenUsage
from ai_supervisor.providers.kimi import KimiResponse, KimiToolCall


class SequenceKimi:
    def __init__(self, responses: list[KimiResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []

    async def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((deepcopy(messages), dict(kwargs)))
        if not self.responses:
            raise AssertionError("unexpected extra Kimi call")
        return self.responses.pop(0)


def _valid_intent_args() -> str:
    return json.dumps(
        {
            "kind": "chat",
            "confidence": 0.95,
            "reason": "обсуждение",
            "clarification": "",
            "task_title_hint": "",
        },
        ensure_ascii=False,
    )


def _agent(kimi: SequenceKimi, settings) -> SupervisorAgent:  # type: ignore[no-untyped-def]
    return SupervisorAgent(kimi, settings, SecretRedactor([]))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_missing_tool_call_retries_with_required_and_preserves_reasoning(settings) -> None:
    kimi = SequenceKimi(
        [
            KimiResponse(
                content="Не буду вызывать инструмент.",
                reasoning_content="r1",
                finish_reason="stop",
                usage=TokenUsage(requests=1),
            ),
            KimiResponse(
                content="",
                reasoning_content="r2",
                tool_calls=[
                    KimiToolCall(id="call-ok", name="submit_intent", arguments=_valid_intent_args())
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(requests=2),
            ),
        ]
    )
    result, usage = await _agent(kimi, settings).classify(
        project_block="demo",
        conversation_block="",
        latest_user_message="обсуди идею",
        conversation_id=1,
    )
    assert result.kind is IntentKind.CHAT
    assert usage.requests == 3
    assert len(kimi.calls) == 2
    assert kimi.calls[0][1]["tool_choice"] == "required"
    assert kimi.calls[1][1]["tool_choice"] == "required"
    retry_messages = kimi.calls[1][0]
    assistant = [m for m in retry_messages if m["role"] == "assistant"]
    assert assistant and assistant[0]["reasoning_content"] == "r1"
    user = [m for m in retry_messages if m["role"] == "user"]
    assert any("не вызвал обязательный инструмент" in str(m["content"]) for m in user)


@pytest.mark.asyncio
async def test_wrong_tool_name_is_answered_and_retried(settings) -> None:
    kimi = SequenceKimi(
        [
            KimiResponse(
                content="",
                reasoning_content="wrong tool reasoning",
                tool_calls=[KimiToolCall(id="wrong-id", name="not_submit_intent", arguments="{}")],
                finish_reason="tool_calls",
                usage=TokenUsage(requests=1),
            ),
            KimiResponse(
                content="",
                reasoning_content="corrected",
                tool_calls=[
                    KimiToolCall(id="call-ok", name="submit_intent", arguments=_valid_intent_args())
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(requests=1),
            ),
        ]
    )
    result, _usage = await _agent(kimi, settings).classify(
        project_block="demo",
        conversation_block="",
        latest_user_message="привет",
        conversation_id=1,
    )
    assert result.kind is IntentKind.CHAT
    retry = kimi.calls[1][0]
    tool_results = [m for m in retry if m["role"] == "tool"]
    assert any(m["tool_call_id"] == "wrong-id" for m in tool_results)
    assert any("Неожиданный инструмент" in str(m["content"]) for m in tool_results)
    assistant = [m for m in retry if m["role"] == "assistant"]
    assert assistant[0]["reasoning_content"] == "wrong tool reasoning"
    assert all(kimi.calls[i][1]["tool_choice"] == "required" for i in range(2))


@pytest.mark.asyncio
async def test_invalid_arguments_retry_and_validate_schema(settings) -> None:
    kimi = SequenceKimi(
        [
            KimiResponse(
                content="",
                reasoning_content="bad json",
                tool_calls=[
                    KimiToolCall(
                        id="bad-args",
                        name="submit_intent",
                        arguments=json.dumps({"kind": "task"}),
                    )
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(requests=1),
            ),
            KimiResponse(
                content="",
                reasoning_content="fixed",
                tool_calls=[
                    KimiToolCall(id="call-ok", name="submit_intent", arguments=_valid_intent_args())
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(requests=1),
            ),
        ]
    )
    result, _usage = await _agent(kimi, settings).classify(
        project_block="demo",
        conversation_block="",
        latest_user_message="привет",
        conversation_id=1,
    )
    assert result.kind is IntentKind.CHAT
    retry = kimi.calls[1][0]
    tool_results = [m for m in retry if m["role"] == "tool"]
    assert any(m["tool_call_id"] == "bad-args" for m in tool_results)
    assert any("не прошли валидацию" in str(m["content"]) for m in tool_results)
    assistant = [m for m in retry if m["role"] == "assistant"]
    assert assistant[0]["reasoning_content"] == "bad json"


@pytest.mark.asyncio
async def test_two_missing_calls_fail_without_infinite_loop(settings) -> None:
    kimi = SequenceKimi(
        [
            KimiResponse(
                content="text only",
                reasoning_content="r",
                finish_reason="stop",
                usage=TokenUsage(requests=1),
            ),
            KimiResponse(
                content="still text",
                reasoning_content="r2",
                finish_reason="stop",
                usage=TokenUsage(requests=1),
            ),
        ]
    )
    with pytest.raises(SupervisorError, match="submit_intent"):
        await _agent(kimi, settings).classify(
            project_block="demo",
            conversation_block="",
            latest_user_message="привет",
            conversation_id=1,
        )
    assert len(kimi.calls) == 2
    assert all(call[1]["tool_choice"] == "required" for call in kimi.calls)


@pytest.mark.asyncio
async def test_chat_uses_auto_without_tools(settings) -> None:
    kimi = SequenceKimi(
        [KimiResponse(content="ответ", reasoning_content="r", usage=TokenUsage(requests=1))]
    )
    response = await _agent(kimi, settings).chat(
        project_block="demo",
        memory_block="",
        summary=None,
        messages=[{"role": "user", "content": "привет"}],
        images=(),
        conversation_id=1,
    )
    assert response.content == "ответ"
    assert len(kimi.calls) == 1
    assert kimi.calls[0][1]["tool_choice"] == "auto"
    assert "tools" not in kimi.calls[0][1]
