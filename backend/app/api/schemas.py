"""Request parsing and validation (hand-written: no Pydantic on this machine, see docs). Every request body is
untrusted input: type-checked, length-limited, and rejected with HTTP 422 if invalid."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..chat.models import Category, EventType


class ValidationFailed(Exception):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(errors)} validation error(s)")
        self.errors = errors


class _Fields:
    """Collects every problem instead of stopping at the first."""

    def __init__(self, data: Any) -> None:
        self.errors: list[dict[str, Any]] = []
        if not isinstance(data, dict):
            self.errors.append({"loc": ["body"], "msg": "must be a JSON object"})
            data = {}
        self._data: dict[str, Any] = data

    def text(self, key: str, *, required: bool = False, default: str | None = "", min_len: int = 0,
             max_len: int = 1000) -> str | None:
        value = self._data.get(key)
        if value is None:
            if required:
                self.errors.append({"loc": [key], "msg": "field required"})
            return default
        if not isinstance(value, str):
            self.errors.append({"loc": [key], "msg": "must be a string"})
            return default
        if not min_len <= len(value) <= max_len:
            self.errors.append({"loc": [key], "msg": f"length must be between {min_len} and {max_len}"})
        return value

    def choice(self, key: str, enum: type, default: Any) -> Any:
        value = self._data.get(key)
        if value is None:
            return default
        try:
            return enum(value)
        except ValueError:
            self.errors.append({"loc": [key], "msg": f"must be one of {[e.value for e in enum]}"})
            return default

    def boolean(self, key: str) -> bool:
        value = self._data.get(key)
        if not isinstance(value, bool):
            self.errors.append({"loc": [key], "msg": "must be true or false"})
            return False
        return value

    def done(self) -> None:
        if self.errors:
            raise ValidationFailed(self.errors)


@dataclass
class SimulateRequest:
    username: str
    message: str = ""
    viewer_id: str | None = None
    message_id: str | None = None  # supply one twice to test de-duplication
    event_type: EventType = EventType.MESSAGE
    amount: str | None = None

    @classmethod
    def parse(cls, data: Any) -> "SimulateRequest":
        f = _Fields(data)
        req = cls(
            username=f.text("username", required=True, default="", min_len=1, max_len=64) or "",
            message=f.text("message", default="", max_len=1000) or "",
            viewer_id=f.text("viewer_id", default=None, min_len=1, max_len=128),
            message_id=f.text("message_id", default=None, min_len=1, max_len=128),
            event_type=f.choice("event_type", EventType, EventType.MESSAGE),
            amount=f.text("amount", default=None, max_len=32),
        )
        f.done()
        return req


@dataclass
class PreviewRequest:
    message: str = ""
    username: str = "viewer"
    category: Category = Category.GENERAL

    @classmethod
    def parse(cls, data: Any) -> "PreviewRequest":
        f = _Fields(data if data is not None else {})
        req = cls(
            message=f.text("message", default="", max_len=500) or "",
            username=f.text("username", default="viewer", min_len=1, max_len=64) or "viewer",
            category=f.choice("category", Category, Category.GENERAL),
        )
        f.done()
        return req


@dataclass
class ModelChange:
    model: str | None  # None only for role "fallback" (= no backup model)
    role: str = "primary"

    @classmethod
    def parse(cls, data: Any) -> "ModelChange":
        f = _Fields(data)
        role = f.text("role", default="primary", max_len=16) or "primary"
        if role not in ("primary", "fallback"):
            f.errors.append({"loc": ["role"], "msg": "must be 'primary' or 'fallback'"})
        model = f.text("model", default=None, max_len=200)
        if role == "primary" and not (model or "").strip():
            f.errors.append({"loc": ["model"], "msg": "field required"})
        f.done()
        return cls(model=(model or "").strip() or None, role=role)


@dataclass
class PersonalitySettings:
    """Edit a personality: how often it may speak (cooldown_seconds, frequency, priority) and what it says (system_prompt,
    description). Any subset; at least one is required."""

    fields: dict[str, Any]

    _LIMITS = {"cooldown_seconds": (0, 86_400, True), "frequency": (0.0, 10.0, False), "priority": (0, 100, True)}
    _TEXT = {"system_prompt": (20, 4000), "description": (0, 300)}

    @classmethod
    def parse(cls, data: Any) -> "PersonalitySettings":
        f = _Fields(data)
        out: dict[str, Any] = {}
        raw = f._data
        for key in raw:
            if key not in cls._LIMITS and key not in cls._TEXT:
                f.errors.append({"loc": [key], "msg": "unknown setting"})
        for key, (lo_len, hi_len) in cls._TEXT.items():
            if key not in raw:
                continue
            value = raw[key]
            if not isinstance(value, str):
                f.errors.append({"loc": [key], "msg": "must be text"})
            elif not lo_len <= len(value.strip()) <= hi_len:
                f.errors.append({"loc": [key], "msg": f"length must be between {lo_len} and {hi_len} characters"})
            else:
                out[key] = value.strip()
        for key, (lo, hi, integer) in cls._LIMITS.items():
            if key not in raw:
                continue
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                f.errors.append({"loc": [key], "msg": "must be a number"})
            elif integer and value != int(value):
                f.errors.append({"loc": [key], "msg": "must be a whole number"})
            elif not lo <= value <= hi:
                f.errors.append({"loc": [key], "msg": f"must be between {lo} and {hi}"})
            else:
                out[key] = int(value) if integer else float(value)
        if not out and not f.errors:
            f.errors.append({"loc": ["body"], "msg": "give at least one of: cooldown_seconds, frequency, priority, system_prompt, description"})
        f.done()
        return cls(out)


@dataclass
class ApprovalDecision:
    """Optional edited text when approving a held message."""

    text: str | None

    @classmethod
    def parse(cls, data: Any) -> "ApprovalDecision":
        f = _Fields(data if data is not None else {})
        text = f.text("text", default=None, min_len=1, max_len=1000)
        f.done()
        return cls(text.strip() if isinstance(text, str) else None)


@dataclass
class StopRequest:
    restart: bool

    @classmethod
    def parse(cls, data: Any) -> "StopRequest":
        f = _Fields(data if data is not None else {})
        value = f._data.get("restart", False)
        if not isinstance(value, bool):
            f.errors.append({"loc": ["restart"], "msg": "must be true or false"})
        f.done()
        return cls(value)


@dataclass
class TicketRedeem:
    ticket: str

    @classmethod
    def parse(cls, data: Any) -> "TicketRedeem":
        f = _Fields(data)
        req = cls(ticket=f.text("ticket", required=True, default="", min_len=1, max_len=128) or "")
        f.done()
        return req


@dataclass
class AIToggle:
    enabled: bool

    @classmethod
    def parse(cls, data: Any) -> "AIToggle":
        f = _Fields(data)
        req = cls(enabled=f.boolean("enabled"))
        f.done()
        return req
