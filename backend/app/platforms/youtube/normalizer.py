"""liveChatMessage (YouTube) -> ChatMessage (platform-neutral).

Event types and detail objects follow the liveChatMessages resource reference (read 2026-11-20). Anything that is not
a viewer message / donation / membership (bans, polls, mode changes, deletions, ...) is ignored.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from ...chat.models import ChatMessage, EventType, Platform

_MAX_ID = 128


def stable_id(raw: str) -> str:
    """Our message-id column holds 128 chars; longer platform ids are replaced by a stable hash. The adapter applies the
    same function to the id returned by insert, so the echo of our own message is recognised as a duplicate."""
    return raw if len(raw) <= _MAX_ID else "h:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_time(value: str | None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def is_chat_ended(item: dict[str, Any]) -> bool:
    return item.get("snippet", {}).get("type") == "chatEndedEvent"


def normalize(item: dict[str, Any], own_channel_id: str | None) -> ChatMessage | None:
    snippet = item.get("snippet") or {}
    author = item.get("authorDetails") or {}
    kind = snippet.get("type")
    channel_id = author.get("channelId") or snippet.get("authorChannelId")
    message_id = item.get("id")
    if not channel_id or not message_id:
        return None

    event, text, amount = EventType.MESSAGE, "", None
    if kind == "textMessageEvent":
        text = (snippet.get("textMessageDetails") or {}).get("messageText") or snippet.get("displayMessage") or ""
    elif kind == "superChatEvent":
        details = snippet.get("superChatDetails") or {}
        event, text, amount = EventType.DONATION, details.get("userComment") or "", details.get("amountDisplayString")
    elif kind == "superStickerEvent":
        event, amount = EventType.DONATION, (snippet.get("superStickerDetails") or {}).get("amountDisplayString")
    elif kind == "newSponsorEvent":
        event = EventType.MEMBERSHIP
    elif kind == "memberMilestoneChatEvent":
        event, text = EventType.MEMBERSHIP, (snippet.get("memberMilestoneChatDetails") or {}).get("userComment") or ""
    elif kind == "membershipGiftingEvent":
        count = (snippet.get("membershipGiftingDetails") or {}).get("giftMembershipsCount")
        event, amount = EventType.MEMBERSHIP, None if count is None else str(count)
    else:
        return None  # bans, polls, deletions, mode changes, gift receipts, ...

    try:
        return ChatMessage(
            platform=Platform.YOUTUBE, viewer_id=channel_id, username=author.get("displayName") or "viewer",
            message=text, timestamp=parse_time(snippet.get("publishedAt")), message_id=stable_id(str(message_id)),
            event_type=event, is_bot_self=bool(own_channel_id) and channel_id == own_channel_id,
            is_streamer=bool(author.get("isChatOwner")), amount=amount,
        )
    except ValueError:
        return None
