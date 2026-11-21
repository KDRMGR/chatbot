"""Read the end of the bot's log file for the dashboard. Lines were already redacted when written; they are redacted again
here as a second guard, so a log viewer can never become a way to read a token."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..logging_setup import redact

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} (\w+)\s+([\w.]+): (.*)$")
MAX_MESSAGE_CHARS = 2000


def tail_log(path: Path, *, max_lines: int = 200, min_level: str = "INFO", max_bytes: int = 512 * 1024) -> list[dict[str, Any]]:
    """The newest ``max_lines`` log entries at or above ``min_level``, oldest first. A line that is not a log record
    (a traceback line) belongs to the entry before it."""
    floor = LEVELS.get(min_level.upper(), 20)
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        raw = handle.read()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line was cut in half
    entries: list[dict[str, Any]] = []
    for line in lines:
        match = _LINE.match(line)
        if match:
            time_, level, logger_name, message = match.groups()
            entries.append({"time": time_, "level": level, "logger": logger_name, "message": message})
        elif entries and len(entries[-1]["message"]) < MAX_MESSAGE_CHARS:
            entries[-1]["message"] += "\n" + line
    kept = [e for e in entries if LEVELS.get(e["level"], 0) >= floor][-max_lines:]
    for entry in kept:
        entry["message"] = redact(entry["message"])[:MAX_MESSAGE_CHARS]
    return kept
