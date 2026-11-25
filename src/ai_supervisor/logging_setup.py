from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ai_supervisor.config import Settings
from ai_supervisor.core.redaction import RedactionFilter, SecretRedactor


def configure_logging(settings: Settings, redactor: SecretRedactor) -> None:
    os.umask(0o077)
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = log_dir / "ai-supervisor.log"
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        os.chmod(log_path, 0o600)
        handlers.append(file_handler)
    except OSError as exc:
        print(f"warning: cannot open log file {log_path}: {exc}", file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactionFilter(redactor))
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, settings.app.log_level, logging.INFO))
    for handler in handlers:
        root.addHandler(handler)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def log_file_path(settings: Settings) -> Path:
    return settings.data_dir / "logs" / "ai-supervisor.log"
