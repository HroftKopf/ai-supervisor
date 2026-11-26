from __future__ import annotations

from ai_supervisor.core.interfaces import IncomingMessage
from ai_supervisor.core.redaction import SecretRedactor
from ai_supervisor.core.request_gateway import build_envelope


def test_request_envelope_is_immutable_and_masks_secrets() -> None:
    redactor = SecretRedactor(["synthetic-secret-value"])
    message = IncomingMessage(
        chat_id=424242,
        user_id=424242,
        text="do work synthetic-secret-value",
        telegram_message_id=77,
    )
    envelope = build_envelope(
        message,
        project_id=3,
        conversation_id=4,
        redactor=redactor,
    )
    assert envelope.request_id.startswith("req-")
    assert envelope.source_message_id == 77
    assert envelope.project_id == 3 and envelope.conversation_id == 4
    assert "synthetic-secret-value" not in envelope.original_text
    assert envelope.mode == "auto"
    assert envelope.source_revision == 1
    assert envelope.content_hash
