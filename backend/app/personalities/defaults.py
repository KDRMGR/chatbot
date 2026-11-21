"""The five default personalities. Prompt text lives in ``prompts/*.md``; the rest is scheduling metadata.
They are seeded into the database on first run; after that the database copy (edited from the dashboard)
is authoritative and is never overwritten."""
from __future__ import annotations

from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_DEFAULTS: list[dict[str, Any]] = [
    {
        "name": "ChatBuddy", "file": "chatbuddy.md",
        "description": "Welcomes viewers, answers questions and keeps the conversation going.",
        "cooldown_seconds": 20, "frequency": 1.0, "priority": 3,
        "categories": ["greeting", "question", "general", "joke"],
    },
    {
        "name": "HypeBot", "file": "hypebot.md",
        "description": "Celebrates exciting gameplay and encourages the streamer.",
        "cooldown_seconds": 30, "frequency": 0.8, "priority": 2,
        "categories": ["gameplay_suggestion", "joke", "proactive_hype"],
    },
    {
        "name": "RoastBot", "file": "roastbot.md",
        "description": "Playful, harmless banter about in-game chaos. Never about people.",
        "cooldown_seconds": 90, "frequency": 0.5, "priority": 1,
        "categories": ["joke", "gameplay_suggestion"],
    },
    {
        "name": "QuizBot", "file": "quizbot.md",
        "description": "Gaming and GTA questions and friendly challenges.",
        "cooldown_seconds": 1500, "frequency": 0.5, "priority": 2,
        "categories": ["proactive_quiz"],
    },
    {
        "name": "StreamBuddy", "file": "streambuddy.md",
        "description": "Starts discussions when chat is quiet and invites community participation.",
        "cooldown_seconds": 300, "frequency": 0.4, "priority": 1,
        "categories": ["general", "proactive_quiet", "proactive_general"],
    },
]


def default_personality_rows() -> list[dict[str, Any]]:
    rows = []
    for spec in _DEFAULTS:
        row = {k: v for k, v in spec.items() if k != "file"}
        row["system_prompt"] = (PROMPTS_DIR / spec["file"]).read_text(encoding="utf-8").strip()
        row["enabled"] = True
        row["configuration"] = {}
        rows.append(row)
    return rows
