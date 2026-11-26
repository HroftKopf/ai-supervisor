from __future__ import annotations

import os

import pytest

from ai_supervisor.agents.supervisor import SupervisorAgent
from ai_supervisor.config import load_settings
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.models import Evidence, IntentKind, ReviewResult, TaskSpec, WorkerResult
from ai_supervisor.providers.kimi import KimiClient, KimiClientConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("AI_SUPERVISOR_RUN_LIVE_KIMI") != "1",
    reason="set AI_SUPERVISOR_RUN_LIVE_KIMI=1 to call the configured Kimi API",
)


@pytest.mark.asyncio
async def test_live_kimi_chat_classify_plan_review() -> None:
    settings = load_settings()
    if not settings.kimi_key:
        pytest.skip("KIMI_API_KEY is not configured")
    redactor = SecretRedactor.from_settings(settings)
    client = KimiClient(
        KimiClientConfig(
            api_key=settings.kimi_key,
            base_url=settings.kimi.base_url,
            model=settings.kimi.model,
            reasoning_effort=settings.kimi.reasoning_effort,
            max_completion_tokens=settings.kimi.max_completion_tokens,
            request_timeout_seconds=settings.kimi.request_timeout_seconds,
            max_retries=0,
            max_concurrent_requests=1,
        ),
        redactor,
    )
    try:
        models = await client.validate_model()
        assert settings.kimi.model in models
        supervisor = SupervisorAgent(client, settings, redactor)

        chat = await supervisor.chat(
            project_block="name: live-smoke",
            memory_block="",
            summary=None,
            messages=[
                {"role": "user", "content": "Ответь одним коротким предложением: связь работает."}
            ],
            images=(),
            conversation_id=900001,
        )
        assert chat.content.strip()
        assert chat.usage.requests == 1
        print("live chat:", chat.content.strip()[:120].replace("\n", " "))

        intent, _ = await supervisor.classify(
            project_block="name: live-smoke",
            conversation_block="",
            latest_user_message="Перепиши README проекта на русский язык.",
            conversation_id=900001,
        )
        assert intent.kind is IntentKind.TASK
        print("live classify:", intent.kind.value, intent.confidence)

        spec, _ = await supervisor.plan(
            project_block="name: live-smoke",
            conversation_block="Пользователь: перепиши README",
            memory_block="",
            project_context="README.md: # Demo\nhello\n",
            latest_user_message="Перепиши README проекта на русский язык и сохрани смысл.",
            conversation_id=900001,
        )
        assert isinstance(spec, TaskSpec)
        assert spec.acceptance_criteria
        print("live plan:", spec.title[:120], "criteria", len(spec.acceptance_criteria))

        evidence = Evidence(
            task_id=900001,
            base_commit="a" * 40,
            head_commit="b" * 40,
            status_porcelain=" M README.md",
            diff_stat=" README.md | 2 +-",
            diff=(
                "diff --git a/README.md b/README.md\n"
                "--- a/README.md\n+++ b/README.md\n@@ -1,2 +1,2 @@\n-# Demo\n-hello\n+# Демо\n+привет\n"
            ),
            changed_files=["README.md"],
            checks=[],
            worker=WorkerResult(
                session_id="live-smoke",
                final_response="README переведён, тесты не настроены.",
                finish_reason="completed",
                return_code=0,
            ),
            verification_note="В проекте не настроены тесты; проверка ограничена diff.",
        )
        review, _ = await supervisor.review(
            project_block="name: live-smoke",
            spec=spec,
            evidence=evidence,
            project_context="README.md: # Demo\nhello\n",
        )
        assert isinstance(review, ReviewResult)
        assert review.verdict.value in {"accept", "rework", "fail"}
        print("live review:", review.verdict.value, review.summary[:120].replace("\n", " "))
    finally:
        await client.close()
