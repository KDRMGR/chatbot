"""The decision: which stream the bot should be in on each platform. Pure functions (no I/O, no clock of their own),
so every rule is tested directly.

Rules, per platform:
  * A stream you SELECTED (pinned) that is live wins.
  * If it is offline and automatic selection is on (the fail-safe), the bot waits ``failover_after_s`` seconds (it may be
    just starting) and then moves to the best live stream on the list; it moves back as soon as the selected one is live.
  * With nothing selected and automatic selection on, the bot stays in the stream it is already in while that is live,
    otherwise starts with the live stream that has the highest priority (list order breaks ties).
  * With automatic selection OFF the bot only ever uses the stream you selected, on that stream's platform.
  * Kick has exactly one stream the bot can read (the signed-in channel), so it is simply on or off: it is never
    switched off because the channel is offline (the chat connection is what notices the stream starting).
A live stream must fail ``DOWN_PROBES`` checks in a row before it counts as offline (one failed check is not an outage).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .model import PLATFORMS, StreamsConfig, StreamTarget

DOWN_PROBES = 2
ALWAYS_ON = frozenset({"kick"})


@dataclass(frozen=True)
class Live:
    """The result of one check. ``live`` is None when the check itself failed (that says nothing about the stream)."""
    live: bool | None
    video_id: str | None = None
    title: str = ""
    error: str | None = None


@dataclass
class TargetState:
    live: bool = False
    down_streak: int = 0
    offline_since: float | None = None  # when it stopped being live (or when we first watched it); None while live
    video_id: str | None = None
    title: str = ""
    error: str | None = None
    checked_at: float | None = None


@dataclass(frozen=True)
class Decision:
    target_id: str | None
    reason: str


def observe(state: TargetState, info: Live, now: float) -> None:
    state.checked_at = now
    if info.live is None:
        state.error = info.error or "the check failed"
        return
    state.error = None
    if info.live:
        state.live, state.down_streak, state.offline_since = True, 0, None
        state.video_id = info.video_id or state.video_id
        state.title = info.title or state.title
        return
    if state.live:
        state.down_streak += 1
        if state.down_streak < DOWN_PROBES:
            return
    state.live, state.down_streak, state.video_id = False, 0, None
    if state.offline_since is None:
        state.offline_since = now


def _ranked(targets: Iterable[StreamTarget]) -> list[StreamTarget]:
    ordered = list(targets)
    return sorted(ordered, key=lambda t: (-t.priority, ordered.index(t)))


def choose(cfg: StreamsConfig, states: dict[str, TargetState], previous: dict[str, str | None],
           now: float) -> dict[str, Decision]:
    def live(t: StreamTarget) -> bool:
        return states.get(t.id, TargetState()).live

    pinned = cfg.target(cfg.pinned)
    if pinned is not None and not pinned.enabled:
        pinned = None
    out: dict[str, Decision] = {}
    for platform in PLATFORMS:
        targets = [t for t in cfg.targets if t.platform == platform and t.enabled]
        if not targets:
            out[platform] = Decision(None, "no stream is switched on for this platform")
            continue
        if not cfg.auto_select and (pinned is None or pinned.platform != platform):
            out[platform] = Decision(None, "automatic selection is off and the stream you selected is on another platform"
                                     if pinned else "automatic selection is off and no stream is selected")
            continue
        if platform in ALWAYS_ON:
            out[platform] = Decision(targets[0].id, "the signed-in channel is always watched")
            continue

        best = next(iter(_ranked(t for t in targets if live(t))), None)
        prev_id = previous.get(platform)
        if pinned is not None and pinned.platform == platform:
            st = states.get(pinned.id, TargetState())
            if st.live:
                out[platform] = Decision(pinned.id, "selected by you")
                continue
            if not cfg.auto_select:
                out[platform] = Decision(None, "waiting for the stream you selected to go live (automatic selection is off)")
                continue
            offline_for = now - (st.offline_since if st.offline_since is not None else now)
            if offline_for < cfg.failover_after_s:
                prev = cfg.target(prev_id)
                if prev is not None and prev.enabled and prev.id != pinned.id and prev.platform == platform and live(prev):
                    out[platform] = Decision(prev.id, "staying here; waiting for the stream you selected to go live")
                else:
                    left = int(cfg.failover_after_s - offline_for)
                    out[platform] = Decision(None, f"waiting for the stream you selected to go live ({left} s before using another)")
                continue
            if best is not None:
                out[platform] = Decision(best.id, "the stream you selected is offline: using a live stream from your list")
            else:
                out[platform] = Decision(None, "the stream you selected is offline and no other stream on your list is live")
            continue
        # nothing selected on this platform (automatic selection is on here)
        prev = cfg.target(prev_id)
        if prev is not None and prev.enabled and prev.platform == platform and live(prev):
            out[platform] = Decision(prev.id, "still live")
        elif best is not None:
            out[platform] = Decision(best.id, "automatic: the live stream with the highest priority")
        else:
            out[platform] = Decision(None, "no stream on your list is live")
    return out
