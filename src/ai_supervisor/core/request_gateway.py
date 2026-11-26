from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_supervisor.core.interfaces import IncomingMessage
from ai_supervisor.core.redaction import SecretRedactor


class RequestTooLarge(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    request_id: str
    correlation_id: str
    owner_id: int
    chat_id: int
    conversation_id: int
    project_id: int
    source_message_id: int | None
    original_text: str
    attachment_refs: tuple[str, ...]
    timestamp: datetime
    mode: str
    source_revision: int
    dedup_key: str = ""
    policy_id: str = "owner-v1"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.original_text.encode("utf-8")).hexdigest()


def build_envelope(
    message: IncomingMessage,
    *,
    project_id: int,
    conversation_id: int,
    redactor: SecretRedactor,
    mode: str = "auto",
    idempotency_key: str | None = None,
) -> RequestEnvelope:
    text = redactor.redact((message.text or "").strip())
    if len(text) > 8000:
        raise RequestTooLarge(
            "Запрос превышает 8000 символов; сократите его или разбейте на задачи."
        )
    request_id = f"req-{uuid.uuid4().hex}"
    if idempotency_key:
        dedup_key = idempotency_key
    elif message.telegram_message_id is not None:
        dedup_key = f"telegram:{message.chat_id}:{message.telegram_message_id}:{mode}"
    else:
        # Internal/non-Telegram callers may omit a source id; preserve a
        # correlation key but do not claim durable deduplication for them.
        dedup_key = f"internal:{uuid.uuid4().hex}"
    return RequestEnvelope(
        request_id=request_id,
        correlation_id=request_id,
        owner_id=message.user_id,
        chat_id=message.chat_id,
        conversation_id=conversation_id,
        project_id=project_id,
        source_message_id=message.telegram_message_id,
        original_text=text,
        attachment_refs=tuple(attachment.filename for attachment in message.attachments),
        timestamp=datetime.now(UTC),
        mode=mode,
        source_revision=1,
        dedup_key=dedup_key,
    )
