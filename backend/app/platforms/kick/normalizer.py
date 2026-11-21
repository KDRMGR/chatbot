"""Kick webhook payload -> ChatMessage (platform-neutral).

Payload shapes are from Kick's docs (events/event-types.md, read 2026-11-21). All events are version 1. Anything the bot does not
use (follows, moderation, rewards, stream status, metadata) returns None.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Iterable

from ...chat.models import ChatMessage, EventType, Platform
from .webhook import parse_timestamp

CHAT_EVENT = "chat.message.sent"
# event name -> what it means to the bot
SUPPORTED_EVENTS = {
    "chat.message.sent": EventType.MESSAGE,
    "channel.subscription.new": EventType.MEMBERSHIP,
    "channel.subscription.renewal": EventType.MEMBERSHIP,
    "channel.subscription.gifts": EventType.MEMBERSHIP,
    "kicks.gifted": EventType.DONATION,
}
_MAX_ID = 128


def _stable(raw: str) -> str:
    return raw if len(raw) <= _MAX_ID else "h:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _when(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(parse_timestamp(value), timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _user(obj: Any) -> tuple[str, str] | None:
    """(user id, name) from a user object; None for anonymous or malformed ones."""
    if not isinstance(obj, dict) or obj.get("user_id") is None or obj.get("is_anonymous") is True:
        return None
    return str(obj["user_id"]), str(obj.get("username") or "viewer")


def normalize(event_type: str, payload: dict[str, Any], event_message_id: str, own_user_ids: Iterable[int | str] = ()) -> ChatMessage | None:
    kind = SUPPORTED_EVENTS.get(event_type)
    if kind is None:
        return None
    own = {str(i) for i in own_user_ids}
    broadcaster = payload.get("broadcaster") if isinstance(payload.get("broadcaster"), dict) else {}
    try:
        if event_type == CHAT_EVENT:
            sender = _user(payload.get("sender"))
            content = payload.get("content")
            message_id = payload.get("message_id")
            if sender is None or not isinstance(content, str) or not message_id:
                return None
            return ChatMessage(
                platform=Platform.KICK, viewer_id=sender[0], username=sender[1], message=content,
                timestamp=_when(payload.get("created_at")), message_id=_stable(str(message_id)), event_type=kind,
                is_bot_self=sender[0] in own,
                is_streamer=broadcaster.get("user_id") is not None and sender[0] == str(broadcaster.get("user_id")),
            )
        mid = _stable(f"{event_type}:{event_message_id}")
        if event_type in ("channel.subscription.new", "channel.subscription.renewal"):
            who = _user(payload.get("subscriber"))
            text, amount = "", None
        elif event_type == "channel.subscription.gifts":
            who = _user(payload.get("gifter")) or ("anonymous", "Someone")
            giftees = payload.get("giftees")
            text, amount = "", str(len(giftees)) if isinstance(giftees, list) else None
        else:  # kicks.gifted
            who = _user(payload.get("sender"))
            gift = payload.get("gift") if isinstance(payload.get("gift"), dict) else {}
            text = str(gift.get("message") or "")
            amount = f"{gift['amount']} KICKs" if gift.get("amount") is not None else None
        if who is None:
            return None
        return ChatMessage(
            platform=Platform.KICK, viewer_id=who[0], username=who[1], message=text, timestamp=_when(payload.get("created_at")),
            message_id=mid, event_type=kind, is_bot_self=who[0] in own, amount=amount,
        )
    except (ValueError, TypeError):
        return None
