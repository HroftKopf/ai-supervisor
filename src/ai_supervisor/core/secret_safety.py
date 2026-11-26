from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from ai_supervisor.core.redaction import SecretRedactor

_SENSITIVE_NAME = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE|AUTH|COOKIE)",
    re.IGNORECASE,
)
# These are non-secret values that historically lived in .env and are safe to move
# to config.toml. They are never returned by value here.
PERSONAL_ENV_KEYS = {"TELEGRAM_OWNER_ID"}
NON_SECRET_ENV_KEYS = {
    "KIMI_BASE_URL",
    "KIMI_MODEL",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_MODEL",
}


def is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_NAME.search(name))


def read_env_keys(path: Path) -> list[str]:
    """Return variable names only. Values are never returned or logged."""
    if not path.is_file():
        return []
    keys: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    return sorted(set(keys))


def env_presence(path: Path) -> dict[str, str]:
    """Safe status map: 'set' or 'empty'; never values or lengths."""
    if not path.is_file():
        return {}
    status: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            status[key] = "set" if value.strip() else "empty"
    return dict(sorted(status.items()))


def mask(value: str | None) -> str:
    return "[REDACTED]" if value else "[unset]"


def ensure_no_secret_values(text: str, redactor: SecretRedactor) -> str:
    """Defence in depth for diagnostic output."""
    return redactor.redact(text)


def safe_join(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line is not None)
