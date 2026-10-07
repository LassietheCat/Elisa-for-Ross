from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Iterable

from .config import Settings

REDACTED = "[REDACTED]"


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secrets:
            record.msg = self.redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self.redact(record.exc_text)
        return True


def setup_logging(settings: Settings, *, process: str) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_dir / f"{process}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    redact = RedactingFilter(settings.secret_values())

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    for handler in (logging.StreamHandler(), file_handler):
        handler.setFormatter(formatter)
        handler.addFilter(redact)
        root.addHandler(handler)
    os.chmod(log_file, 0o600)
