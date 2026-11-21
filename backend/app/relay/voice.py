"""Hold-to-speak: what the streamer SAYS becomes a message from the streamer, and the bot answers it in chat.

The words are never posted to the platform's chat as the streamer's own message (the viewers hear them on the stream). Instead the
text goes through the normal pipeline as a message FROM the streamer (``is_streamer`` + ``via_voice``): the same filters, the same
personalities and AI, the same output guard, rate limits, pause and approval switches as any viewer message, so the bot's answer
reads like a reaction to what was just said and the conversation continues (the streamer's line is also kept in the bot's short
chat memory, so the next replies know it). Only the per-viewer cooldown is waived for the streamer.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from ..chat.models import ChatMessage, Platform
from ..platforms.base import ConnectionState

ANSWER_TIMEOUT_S = 45.0
_SAY_PLATFORMS = (Platform.KICK, Platform.YOUTUBE)  # where the answer is posted: the first one that is connected
OK_ACTIONS = {"replied", "held"}  # "held" = the reply is waiting for approval in the dashboard


def pick_platform(adapters: dict[Platform, Any]) -> Platform | None:
    for platform in _SAY_PLATFORMS:
        adapter = adapters.get(platform)
        if adapter is not None and adapter.status().state is ConnectionState.CONNECTED:
            return platform
    return None


def make_voice_handler(container: Any):
    """The ``say_handler`` for the relay: ``(text, context) -> result dict`` (``ok`` False when nothing was answered)."""

    async def say(text: str, ctx: dict[str, Any]) -> dict[str, Any]:
        platform = pick_platform(container.adapters)
        if platform is None:
            return {"ok": False, "action": "no_stream", "error": "no stream is connected, so the bot has nowhere to answer"}
        name = container.settings.streamer_name
        msg = ChatMessage(
            platform=platform, viewer_id="voice-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "streamer"),
            username=f"{name} (voice)", message=text, message_id=f"voice-{uuid.uuid4().hex}", is_streamer=True, via_voice=True,
        )
        try:
            result = await container.processor.submit(msg, wait=True, timeout=ANSWER_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"ok": False, "action": "timeout", "error": "the bot took too long to answer"}
        data = result.to_dict() if result is not None else {}
        reply = data.get("reply")
        return {"ok": result is not None and result.action in OK_ACTIONS, "action": getattr(result, "action", "none"),
                "reason": data.get("reason"), "platform": platform.value, "reply": str(reply)[:300] if reply else None}

    return say
