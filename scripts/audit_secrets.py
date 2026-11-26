#!/usr/bin/env python3
"""Report files containing configured secret VALUES without printing the values.

This is a local incident/diagnostic tool. It never prints a secret, never modifies
or deletes logs, and deliberately skips .env itself (which is the authoritative
local secret store).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_supervisor.config import load_settings  # noqa: E402
from ai_supervisor.core.secret_safety import PERSONAL_ENV_KEYS, read_env_keys  # noqa: E402

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "var", "__pycache__", ".pytest_cache"}
SECRET_ENV_NAMES = {
    "KIMI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "GITHUB_TOKEN",
    *PERSONAL_ENV_KEYS,
}


def secret_values(env_path: Path) -> dict[str, str]:
    settings = load_settings(check_env_permissions=False)
    configured = {
        "KIMI_API_KEY": settings.kimi_key,
        "DEEPSEEK_API_KEY": settings.deepseek_key,
        "OPENAI_API_KEY": settings.openai_key,
        "OPENAI_COMPATIBLE_API_KEY": settings.openai_compatible_key,
        "TELEGRAM_BOT_TOKEN": settings.telegram_token,
        "GITHUB_TOKEN": settings.github_token,
    }
    return {name: value for name, value in configured.items() if value}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--max-bytes", type=int, default=20_000_000)
    args = parser.parse_args()
    settings = load_settings(check_env_permissions=False)
    values = secret_values(settings.env_path)
    if not values:
        print("no configured provider credentials found; nothing to scan")
        return 0
    findings: list[tuple[str, list[str]]] = []
    for base, dirs, files in os.walk(args.root):
        dirs[:] = [item for item in dirs if item not in SKIP_DIRS]
        for name in files:
            path = Path(base) / name
            if path.name == ".env" or not path.is_file():
                continue
            try:
                if path.stat().st_size > args.max_bytes:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            hits = sorted(key for key, value in values.items() if value.encode() in data)
            if hits:
                findings.append((str(path.relative_to(args.root)), hits))
    env_keys = set(read_env_keys(settings.env_path))
    unexpected_env = sorted(env_keys - SECRET_ENV_NAMES)
    if unexpected_env:
        print(".env contains non-credential keys: " + ", ".join(unexpected_env))
    for path, keys in findings:
        print(f"{path}: {', '.join(keys)}")
    print(f"scanned={args.root} findings={len(findings)}")
    return 2 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
