"""Logging: rotating file + console, with secret redaction so tokens never reach the logs."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?bearer\s+)[^\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)((?:access|refresh|id)_token|client_secret|api[_-]?key|x-api-key|password|secret)"
            r"(['\"]?\s*[:=]\s*['\"]?)[^\s,'\"&}]+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)([?&](?:access_token|refresh_token|code|key|client_secret)=)[^&\s]+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the app
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO", max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> None:
    """Idempotent: calling twice does not duplicate handlers."""
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        if getattr(handler, "_stealthbot", False):
            root.removeHandler(handler)
            handler.close()

    file_handler = RotatingFileHandler(
        log_dir / "stealthbot.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    console = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, console):
        handler._stealthbot = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
    # httpx logs every request URL at INFO; keep the noise (and any query strings) out
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
