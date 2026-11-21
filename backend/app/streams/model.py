"""Stream targets (the watch list) and the director's settings: validation, parsing of pasted links, saving.

A target is one place the bot may comment:
  youtube / own      the signed-in Google account's live stream (the bot's normal discovery, as before)
  youtube / video    one specific video, found by its 11-character id
  youtube / channel  a channel (UC... id); the live stream of that channel is looked up with YouTube's search
  kick    / own      the signed-in Kick account's channel

Kick channels other than the signed-in one cannot be added: with a user sign-in Kick's API only delivers chat
events for the signed-in account's own channel (swagger: ``broadcaster_user_id`` "will be ignored" for a user token).
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

MAX_TARGETS = 20
PLATFORMS = ("youtube", "kick")
KINDS = {"youtube": ("own", "video", "channel"), "kick": ("own",)}
OWN_IDS = {"youtube": "youtube-own", "kick": "kick-own"}
OWN_LABELS = {"youtube": "YouTube: the signed-in account's live stream", "kick": "Kick: the signed-in account's channel"}

YT_CHANNEL_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
YT_VIDEO_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

LABEL_MAX = 60

# Setting -> (minimum, maximum, default)
SETTING_RANGES: dict[str, tuple[int, int, int]] = {
    "failover_after_s": (0, 86400, 180),
    "poll_interval_s": (15, 3600, 60),
    "youtube_channel_probe_s": (300, 86400, 900),
}


class StreamsError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(str(e.get("msg", "")) for e in errors) or "invalid stream settings")
        self.errors = errors


def _err(field_name: str, msg: str) -> StreamsError:
    return StreamsError([{"loc": [field_name], "msg": msg}])


@dataclass(frozen=True)
class StreamTarget:
    id: str
    platform: str
    kind: str
    ref: str = ""
    label: str = ""
    priority: int = 50  # higher = preferred when several streams on the same platform are live
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "platform": self.platform, "kind": self.kind, "ref": self.ref, "label": self.label,
                "priority": self.priority, "enabled": self.enabled}

    @staticmethod
    def from_dict(data: Any) -> "StreamTarget":
        if not isinstance(data, dict):
            raise ValueError("a stream entry must be an object")
        platform, kind, ref = str(data.get("platform", "")), str(data.get("kind", "")), str(data.get("ref", ""))
        if platform not in PLATFORMS or kind not in KINDS[platform]:
            raise ValueError(f"unknown stream type {platform}/{kind}")
        if kind == "video" and not YT_VIDEO_RE.match(ref):
            raise ValueError("bad video id")
        if kind == "channel" and not YT_CHANNEL_RE.match(ref):
            raise ValueError("bad channel id")
        priority = data.get("priority", 50)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            raise ValueError("bad priority")
        label = clean_label(str(data.get("label", ""))) or default_label(platform, kind, ref)
        tid = str(data.get("id", ""))
        if not re.fullmatch(r"[a-z0-9-]{3,40}", tid):
            raise ValueError("bad id")
        return StreamTarget(tid, platform, kind, ref, label, priority, bool(data.get("enabled", True)))


def clean_label(text: str) -> str:
    return " ".join(str(text).split())[:LABEL_MAX]


def default_label(platform: str, kind: str, ref: str) -> str:
    if kind == "own":
        return OWN_LABELS[platform]
    if kind == "video":
        return f"YouTube video {ref}"
    return f"YouTube channel {ref}"


def parse_youtube_ref(text: str) -> tuple[str, str]:
    """A pasted YouTube link or id -> ("video" | "channel", id). Raises StreamsError with a plain explanation."""
    raw = (text or "").strip()
    if not raw:
        raise _err("ref", "paste a YouTube video link, a channel link (youtube.com/channel/UC...) or an id")
    if raw.startswith("@") or "/@" in raw:
        raise _err("ref", "channel handles (@name) cannot be looked up here: open the channel, copy its "
                          "youtube.com/channel/UC... address (or the UC... id) and paste that")
    candidate = raw
    if "/" in raw or "." in raw:
        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        host = (parsed.hostname or "").lower()
        if host not in _YT_HOSTS:
            raise _err("ref", "that is not a YouTube address")
        parts = [p for p in parsed.path.split("/") if p]
        if host.endswith("youtu.be"):
            candidate = parts[0] if parts else ""
        elif parts and parts[0] == "watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in ("live", "embed", "shorts", "v"):
            candidate = parts[1]
        elif len(parts) >= 2 and parts[0] == "channel":
            candidate = parts[1]
        else:
            raise _err("ref", "that address is not a video or a /channel/UC... link")
    if YT_CHANNEL_RE.match(candidate):
        return "channel", candidate
    if YT_VIDEO_RE.match(candidate):
        return "video", candidate
    raise _err("ref", "not a valid YouTube video id (11 characters) or channel id (UC + 22 characters)")


def new_target_id(platform: str, kind: str, existing: set[str]) -> str:
    while True:
        tid = f"{platform[:2]}-{kind[:2]}-{secrets.token_hex(3)}"
        if tid not in existing:
            return tid


@dataclass
class StreamsConfig:
    targets: list[StreamTarget] = field(default_factory=list)
    pinned: str | None = None
    auto_select: bool = True  # the fail-safe: with nothing selected (or the selection offline) use the live stream on the list
    failover_after_s: int = SETTING_RANGES["failover_after_s"][2]
    poll_interval_s: int = SETTING_RANGES["poll_interval_s"][2]
    youtube_channel_probe_s: int = SETTING_RANGES["youtube_channel_probe_s"][2]

    def target(self, target_id: str | None) -> StreamTarget | None:
        return next((t for t in self.targets if t.id == target_id), None) if target_id else None

    def to_saved(self) -> dict[str, Any]:
        return {"targets": [t.to_dict() for t in self.targets], "pinned": self.pinned, "auto_select": self.auto_select,
                "failover_after_s": self.failover_after_s, "poll_interval_s": self.poll_interval_s,
                "youtube_channel_probe_s": self.youtube_channel_probe_s}

    @staticmethod
    def from_saved(data: Any) -> "StreamsConfig":
        """Tolerant on purpose: unreadable entries are dropped, never a reason for the bot not to start."""
        cfg = StreamsConfig()
        if not isinstance(data, dict):
            return cfg
        seen: set[str] = set()
        for raw in data.get("targets") or []:
            try:
                t = StreamTarget.from_dict(raw)
            except (ValueError, TypeError):
                continue
            if t.id not in seen and len(cfg.targets) < MAX_TARGETS:
                seen.add(t.id)
                cfg.targets.append(t)
        pinned = data.get("pinned")
        cfg.pinned = pinned if isinstance(pinned, str) and pinned in seen else None
        cfg.auto_select = bool(data.get("auto_select", True))
        for key, (lo, hi, default) in SETTING_RANGES.items():
            value = data.get(key, default)
            setattr(cfg, key, value if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi else default)
        return cfg

    def validated_settings(self, body: Any) -> dict[str, Any]:
        """Check a settings change; return the new values (nothing is applied here). All or nothing."""
        if not isinstance(body, dict):
            raise _err("body", "must be an object")
        errors: list[dict[str, Any]] = []
        new: dict[str, Any] = {}
        for key, value in body.items():
            if key == "auto_select":
                if isinstance(value, bool):
                    new[key] = value
                else:
                    errors.append({"loc": [key], "msg": "must be true or false"})
            elif key in SETTING_RANGES:
                lo, hi, _ = SETTING_RANGES[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
                    errors.append({"loc": [key], "msg": "must be a whole number of seconds"})
                elif not lo <= value <= hi:
                    errors.append({"loc": [key], "msg": f"must be between {lo} and {hi} seconds"})
                else:
                    new[key] = int(value)
            else:
                errors.append({"loc": [key], "msg": "unknown setting"})
        if errors:
            raise StreamsError(errors)
        return new
