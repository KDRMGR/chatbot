"""Runtime switches. Plain booleans read on every message, so they work instantly and independently of the AI."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RuntimeControls:
    outgoing_paused: bool = False  # emergency stop for ALL outgoing messages
    ai_enabled: bool = True  # False = rule-based replies only
    proactive_enabled: bool = True  # False = the bot only answers viewers, it never starts a conversation
    approval_required: bool = False  # True = every message the bot composes waits for the streamer's approval
