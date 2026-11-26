from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from ai_supervisor.cli import run_config_audit
from ai_supervisor.core.redaction import RedactionFilter, SecretRedactor
from ai_supervisor.core.secret_safety import env_presence, read_env_keys


def test_redactor_covers_all_provider_and_github_secrets(settings) -> None:
    settings.secrets = settings.secrets.model_copy(
        update={
            "kimi_api_key": SecretStr("kimi-secret-value"),
            "deepseek_api_key": SecretStr("deepseek-secret-value"),
            "openai_api_key": SecretStr("openai-secret-value"),
            "openai_compatible_api_key": SecretStr("compatible-secret-value"),
            "telegram_bot_token": SecretStr("123456789:telegram-secret-value-1234567890"),
            "github_token": SecretStr("github-secret-value"),
        }
    )
    redactor = SecretRedactor.from_settings(settings)
    text = (
        "KIMI_API_KEY=kimi-secret-value\n"
        "Authorization: Bearer openai-secret-value\n"
        "github_pat_abcdefghijklmnopqrstuvwxyz123456\n"
        "TELEGRAM_BOT_TOKEN=123456789:telegram-secret-value-1234567890"
    )
    cleaned = redactor.redact(text)
    for secret in (
        "kimi-secret-value",
        "deepseek-secret-value",
        "openai-secret-value",
        "compatible-secret-value",
        "github-secret-value",
        "telegram-secret-value",
        "github_pat_abcdefghijklmnopqrstuvwxyz123456",
    ):
        assert secret not in cleaned


def test_logging_filter_redacts_message_args_stack_and_exception() -> None:
    redactor = SecretRedactor(["super-secret-value"])
    record = logging.LogRecord(
        "test", logging.ERROR, __file__, 1, "failed with %s", ("super-secret-value",), None
    )
    record.stack_info = "stack super-secret-value"
    RedactionFilter(redactor).filter(record)
    assert "super-secret-value" not in record.getMessage()
    assert "super-secret-value" not in str(record.stack_info)


def test_safe_env_reader_never_returns_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=super-secret-token\n"
        "TELEGRAM_OWNER_ID=123\n"
        "DEEPSEEK_API_KEY=deepseek-secret\n",
        encoding="utf-8",
    )
    keys = read_env_keys(env)
    status = env_presence(env)
    assert keys == ["DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"]
    assert status == {
        "DEEPSEEK_API_KEY": "set",
        "TELEGRAM_BOT_TOKEN": "set",
        "TELEGRAM_OWNER_ID": "set",
    }
    assert "deepseek-secret" not in repr(keys) + repr(status)


def test_config_audit_output_has_no_secret_values(settings, capsys) -> None:
    settings.secrets = settings.secrets.model_copy(
        update={"kimi_api_key": SecretStr("audit-secret-value")}
    )
    code = run_config_audit(settings)
    captured = capsys.readouterr()
    assert "audit-secret-value" not in captured.out + captured.err
    assert "KIMI_API_KEY" in captured.out
    assert code in {0, 1}


@pytest.mark.asyncio
async def test_conversation_storage_and_api_context_redact_secrets(
    database, settings, project
) -> None:
    from ai_supervisor.memory.conversations import ConversationService

    settings.secrets = settings.secrets.model_copy(
        update={"kimi_api_key": SecretStr("kimi-secret-value")}
    )
    service = ConversationService(database, settings)
    conversation = await service.get_or_create_conversation(424242, project.id)
    message = await service.add_message(
        conversation.id, "user", "my key is kimi-secret-value and token x"
    )
    assert "kimi-secret-value" not in message.content
    api = service.messages_for_api([message])
    assert "kimi-secret-value" not in str(api)
