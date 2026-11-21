"""Live-editable AI parameters and message limits (spec dashboard sections D and F).

Both are plain numbers with hard limits, validated here in one place and applied to the running ``Settings`` object (every
component reads its settings on each use, so a change takes effect on the next message). The application saves the result in
the database and re-applies it at the next start.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from ..config import RateLimitConfig, Settings

# name -> (minimum, maximum, whole number?)
AI_FIELDS: dict[str, tuple[float, float, bool]] = {
    "num_ctx": (512, 8192, True),  # context window. The model is reloaded when it changes; keep it small on a 4 GB GPU
    "num_predict": (16, 400, True),  # most tokens the model may write per reply
    "temperature": (0.0, 2.0, False),
    "generation_timeout_s": (3.0, 180.0, False),  # one attempt; the whole call (one retry, then the backup model) gets twice that
    "reply_max_words": (10, 80, True),  # longest reply the output guard accepts, also what the prompt asks for
}
RATE_FIELDS: dict[str, tuple[float, float, bool]] = {
    "min_gap_s": (0.0, 3600.0, False),  # seconds between any two bot messages on that platform
    "max_per_window": (1, 10_000, True),  # at most this many bot messages per window
    "window_s": (10.0, 86_400.0, False),
    "user_cooldown_s": (0.0, 3600.0, False),  # do not answer the same viewer more often than this
    "max_chars": (20, 2000, True),  # message length limit of the platform
}


class SettingsError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(errors)} invalid setting(s)")
        self.errors = errors


def parse_numbers(data: Any, spec: Mapping[str, tuple[float, float, bool]]) -> dict[str, float | int]:
    """Validate a partial update. Any subset of the known keys; at least one; every problem is reported."""
    if not isinstance(data, Mapping):
        raise SettingsError([{"loc": ["body"], "msg": "must be a JSON object"}])
    errors: list[dict[str, Any]] = []
    out: dict[str, float | int] = {}
    for key in data:
        if key not in spec:
            errors.append({"loc": [key], "msg": f"unknown setting (known: {sorted(spec)})"})
    for key, (low, high, integer) in spec.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append({"loc": [key], "msg": "must be a number"})
        elif integer and value != int(value):
            errors.append({"loc": [key], "msg": "must be a whole number"})
        elif not low <= value <= high:
            errors.append({"loc": [key], "msg": f"must be between {low:g} and {high:g}"})
        else:
            out[key] = int(value) if integer else float(value)
    if not out and not errors:
        errors.append({"loc": ["body"], "msg": f"give at least one of: {', '.join(spec)}"})
    if errors:
        raise SettingsError(errors)
    return out


def ai_values(settings: Settings) -> dict[str, float | int]:
    return {key: getattr(settings, key) for key in AI_FIELDS}


def apply_ai(settings: Settings, values: Mapping[str, float | int]) -> None:
    for key, value in values.items():
        setattr(settings, key, value)
    if "generation_timeout_s" in values:  # the whole call (retry + backup model) must be able to hold two attempts
        settings.generation_deadline_s = max(2 * settings.generation_timeout_s, settings.generation_timeout_s + 10)


def rate_values(config: RateLimitConfig) -> dict[str, float | int]:
    return {key: getattr(config, key) for key in RATE_FIELDS}


def apply_rate_limits(settings: Settings, platform: str, values: Mapping[str, float | int]) -> RateLimitConfig:
    new = replace(settings.limits_for(platform), **values)
    settings.rate_limits[platform] = new
    return new
