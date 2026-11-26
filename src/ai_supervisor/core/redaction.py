from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, quote_plus

from ai_supervisor.config import Settings

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer / Authorization values
    re.compile(r"(?i)\b(authorization|bearer)\b(\s*[:=]?\s*)(?:bearer\s+)?\S+"),
    # Common API key shapes
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    # Telegram bot token
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    # PEM private key material
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    # Env assignments of sensitive names
    re.compile(
        r"(?im)^\s*([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*['\"]?[^\s'\"]{6,}['\"]?"
    ),
)


class SecretRedactor:
    def __init__(self, explicit_secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted(
            {value for value in explicit_secrets if value and len(value) >= 6},
            key=len,
            reverse=True,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretRedactor:
        return cls(
            (
                settings.kimi_key,
                settings.openai_key,
                settings.openai_compatible_key,
                settings.deepseek_key,
                settings.telegram_token,
                settings.github_token,
            )
        )

    def redact(self, text: str) -> str:
        if not text:
            return text
        result = text
        for secret in self._secrets:
            result = result.replace(secret, "[REDACTED]")
            # Common URL encodings must not bypass redaction. This is not a
            # universal encoding detector; arbitrary transforms are not claimed.
            for encoded in (quote(secret, safe=""), quote_plus(secret)):
                if encoded != secret:
                    result = result.replace(encoded, "[REDACTED]")
        for pattern in _PATTERNS:
            result = pattern.sub(_replace_match, result)
        return result

    def redact_mapping(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {str(key): self._redact_value(item) for key, item in value.items()}

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return self.redact_mapping(value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item) for item in value)
        return value


def _replace_match(match: re.Match[str]) -> str:
    original = match.group(0)
    if original.lower().startswith(("bearer", "authorization")):
        prefix = match.group(1) + match.group(2)
        return prefix + "[REDACTED]"
    if "=" in original or ":" in original:
        key, separator, _ = original.partition("=" if "=" in original else ":")
        return f"{key}{separator}[REDACTED]"
    return "[REDACTED]"


class RedactionFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redactor.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: self.redactor.redact(str(value)) for key, value in record.args.items()
                }
            else:
                record.args = tuple(self.redactor.redact(str(value)) for value in record.args)
        if record.stack_info:
            record.stack_info = self.redactor.redact(str(record.stack_info))
        if record.exc_info:
            # Tracebacks can contain request bodies; keep the type but redact text.
            exc = record.exc_info[1]
            if exc is not None:
                record.exc_text = self.redactor.redact(f"{type(exc).__name__}: {exc}")
        return True


def install_redaction(redactor: SecretRedactor) -> None:
    """Apply the redactor to all existing and future logging handlers."""
    logging.setLogRecordFactory(_redacting_factory(redactor))
    for logger_name in (None, *logging.root.manager.loggerDict):
        logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
        for handler in logger.handlers:
            if not any(isinstance(existing, RedactionFilter) for existing in handler.filters):
                handler.addFilter(RedactionFilter(redactor))


_original_factory = logging.getLogRecordFactory()


def _redacting_factory(redactor: SecretRedactor):
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = _original_factory(*args, **kwargs)
        if isinstance(record.msg, str):
            record.msg = redactor.redact(record.msg)
        return record

    return factory


class StreamingRedactor:
    """Safely redact chunked text.

    A secret can be split across arbitrary chunk boundaries. Emitting a processed
    prefix would be unsafe unless a full streaming DFA is implemented, so chunks
    are buffered and redacted once at flush(). This preserves confidentiality at
    the cost of no incremental display; callers that need live display must use a
    bounded, non-secret transport.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        self.redactor = redactor
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        self._buffer += chunk or ""
        return ""

    def flush(self) -> str:
        value = self.redactor.redact(self._buffer)
        self._buffer = ""
        return value
