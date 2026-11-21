"""The stream director: keeps the watch list, checks which streams are live, and points the platform adapters at the
right one (see ``choose.py`` for the rules). It never sends or reads chat itself.

What it does to the adapters, and only when its decision CHANGES (so a manual Connect/Disconnect in the dashboard is
not fought over):
  YouTube  ``retarget(video_id)`` moves the adapter to that live video (``None`` = its normal discovery of the signed-in
           account's stream); with no live stream and no "own" target switched on it stops polling.
  Kick     start / stop (the signed-in channel is the only one the bot can read, see ``model.py``).
Checks cost YouTube quota: own stream = read from the adapter (free) or ``liveBroadcasts.list`` (1 unit) while the
adapter is elsewhere; a video = ``videos.list`` (1 unit); a channel = ``search.list`` (own 100-calls/day bucket), so channels
are checked at most every ``youtube_channel_probe_s`` (times the number of channels), and every call goes through the
quota guard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from ..platforms.base import ConnectionState
from .choose import Decision, Live, TargetState, choose, observe
from .model import (
    KINDS, MAX_TARGETS, OWN_IDS, PLATFORMS, StreamsConfig, StreamsError, StreamTarget, clean_label, default_label,
    new_target_id, parse_youtube_ref,
)

logger = logging.getLogger(__name__)
LogEvent = Callable[[str, str], Awaitable[None]]
SaveConfig = Callable[[dict[str, Any]], Awaitable[None]]
_DEFAULT_YT = ("default",)


class _Skip(Exception):
    """Nothing to learn this round (a throttled check): keep the previous state."""


class StreamDirector:
    def __init__(
        self, *, youtube: Any = None, youtube_api: Any = None, quota: Any = None, kick: Any = None,
        save: SaveConfig | None = None, log_event: LogEvent | None = None,
        persist_quota: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic, first_delay_s: float = 15.0,
    ) -> None:
        self._yt, self._yt_api, self._quota, self._kick = youtube, youtube_api, quota, kick
        self._save, self._log_event, self._persist_quota = save, log_event, persist_quota
        self._clock, self._first_delay_s = clock, first_delay_s
        self.config = StreamsConfig()
        self.states: dict[str, TargetState] = {}
        self.decisions: dict[str, Decision] = {}
        self._current: dict[str, str | None] = {}
        self._applied: dict[str, tuple[Any, ...]] = {"youtube": _DEFAULT_YT, "kick": ("running",)}
        self._last_search: dict[str, float] = {}
        self._last_logged: dict[str, str | None] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._bg: set[asyncio.Task[Any]] = set()
        self.ingest: Any = None  # set by main: what adapters deliver messages to (needed to start one again)
        self.last_tick_at: float | None = None

    # ------------------------------------------------------------------ availability / loading / saving
    def available(self) -> dict[str, bool]:
        return {"youtube": self._yt is not None, "kick": self._kick is not None}

    async def load(self, raw: str | None) -> None:
        try:
            self.config = StreamsConfig.from_saved(json.loads(raw)) if raw else StreamsConfig()
        except ValueError:
            logger.warning("ignoring unreadable saved stream list")
            self.config = StreamsConfig()
        changed = self._ensure_own_targets()
        now = self._clock()
        self.states = {t.id: TargetState(offline_since=now) for t in self.config.targets}
        if changed:
            await self._persist()

    def _ensure_own_targets(self) -> bool:
        """Every connected platform always has its 'own stream' entry, so the bot behaves as before with no setup."""
        changed = False
        for platform, ok in self.available().items():
            if ok and not any(t.platform == platform and t.kind == "own" for t in self.config.targets):
                self.config.targets.insert(0 if platform == "youtube" else len(self.config.targets), StreamTarget(
                    OWN_IDS[platform], platform, "own", "", default_label(platform, "own", ""), 50, True))
                changed = True
        return changed

    async def _persist(self) -> None:
        if self._save:
            try:
                await self._save(self.config.to_saved())
            except Exception:  # noqa: BLE001 - bookkeeping must never break the bot
                logger.exception("could not save the stream list")

    # ------------------------------------------------------------------ the loop
    async def run(self) -> None:
        if self._first_delay_s:
            await asyncio.sleep(self._first_delay_s)  # let the adapters connect before the first look
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                logger.exception("stream director check failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.config.poll_interval_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> None:
        """Check every switched-on stream, then re-decide."""
        for t in [t for t in self.config.targets if t.enabled]:
            state = self.states.setdefault(t.id, TargetState(offline_since=self._clock()))
            try:
                info = await self._probe(t, state)
            except _Skip:
                continue
            observe(state, info, self._clock())
        self.last_tick_at = self._clock()
        await self.reevaluate()

    async def reevaluate(self) -> None:
        async with self._lock:
            decisions = choose(self.config, self.states, self._current, self._clock())
            for platform in PLATFORMS:
                await self._apply(platform, decisions[platform])
            self.decisions = decisions
            self._current = {p: d.target_id for p, d in decisions.items()}
            await self._log_changes(decisions)

    # ------------------------------------------------------------------ probes
    def _guard(self, method: str) -> None:
        if self._quota is not None and not self._quota.can_read(method):
            raise _Failed("YouTube quota guard: check skipped")

    async def _probe(self, t: StreamTarget, state: TargetState) -> Live:
        try:
            if t.platform == "kick":
                return self._probe_kick()
            return await self._probe_youtube(t, state)
        except _Skip:
            raise
        except _Failed as exc:
            return Live(None, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed check says nothing about the stream
            return Live(None, error=str(getattr(exc, "reason", None) or exc.__class__.__name__)[:80])

    def _probe_kick(self) -> Live:
        if self._kick is None:
            return Live(None, error="Kick is not enabled")
        status = self._kick.status()
        if status.state is ConnectionState.ERROR:
            return Live(None, error=status.detail or "Kick connection problem")
        is_live = self._kick.details().get("is_live")
        return Live(bool(is_live)) if is_live is not None else Live(None, error="not known yet")

    def _yt_connected(self) -> bool:
        return self._yt is not None and self._yt.status().state is ConnectionState.CONNECTED

    async def _probe_youtube(self, t: StreamTarget, state: TargetState) -> Live:
        if self._yt is None or self._yt_api is None:
            return Live(None, error="YouTube is not enabled")
        applied = self._applied["youtube"]
        on_video = applied[1] if applied[0] == "video" else None
        connected = self._yt_connected()
        if t.kind == "own":
            if applied == _DEFAULT_YT:  # the adapter's own discovery already tells us: no API call
                status = self._yt.status()
                if status.state is ConnectionState.ERROR:
                    return Live(None, error=status.detail or "YouTube connection problem")
                return Live(True, video_id=self._yt.details().get("video_id")) if connected else Live(False)
            self._guard("liveBroadcasts.list")
            broadcast = await self._yt_api.active_broadcast()
            await self._save_quota()
            return Live(True, video_id=broadcast.video_id, title=broadcast.title) if broadcast else Live(False)
        if t.kind == "video":
            if on_video == t.ref and connected:
                return Live(True, video_id=t.ref)
            self._guard("videos.list")
            chat_id = await self._yt_api.video_chat_id(t.ref)
            await self._save_quota()
            return Live(True, video_id=t.ref) if chat_id else Live(False)
        # channel: search is expensive, so check rarely
        if on_video and on_video == state.video_id and connected:
            return Live(True, video_id=on_video)
        channels = sum(1 for x in self.config.targets if x.enabled and x.kind == "channel") or 1
        every = self.config.youtube_channel_probe_s * channels
        now = self._clock()
        if now - self._last_search.get(t.id, float("-inf")) < every:
            raise _Skip
        self._guard("search.list")
        self._last_search[t.id] = now
        video_id = await self._yt_api.find_live_video(t.ref)
        await self._save_quota()
        if not video_id:
            return Live(False)
        self._guard("videos.list")
        chat_id = await self._yt_api.video_chat_id(video_id)
        await self._save_quota()
        return Live(True, video_id=video_id) if chat_id else Live(False)

    async def _save_quota(self) -> None:
        if self._persist_quota:
            await self._persist_quota()

    # ------------------------------------------------------------------ applying decisions (edge-triggered)
    def _own_enabled(self, platform: str) -> bool:
        return any(t.platform == platform and t.kind == "own" and t.enabled for t in self.config.targets)

    def _desired_youtube(self, d: Decision) -> tuple[Any, ...]:
        target = self.config.target(d.target_id)
        if target is None:  # nothing to follow: keep looking for the signed-in account's stream, unless strict mode says idle
            return _DEFAULT_YT if (self._own_enabled("youtube") and self.config.auto_select) else ("stopped",)
        if target.kind == "own":
            return _DEFAULT_YT
        video = target.ref if target.kind == "video" else self.states.get(target.id, TargetState()).video_id
        return ("video", video) if video else _DEFAULT_YT

    async def _apply(self, platform: str, d: Decision) -> None:
        try:
            if platform == "youtube" and self._yt is not None:
                desired = self._desired_youtube(d)
                if desired != self._applied["youtube"]:
                    previous = self._applied["youtube"]
                    if desired[0] == "stopped":
                        await self._yt.stop()
                    else:
                        await self._yt.retarget(desired[1] if desired[0] == "video" else None)
                        if previous[0] == "stopped" and self.ingest is not None:
                            await self._yt.start(self.ingest)
                    self._applied["youtube"] = desired
            elif platform == "kick" and self._kick is not None:
                desired_k = ("running",) if d.target_id else ("stopped",)
                if desired_k != self._applied["kick"]:
                    if desired_k[0] == "stopped":
                        await self._kick.stop()
                    elif self.ingest is not None:
                        await self._kick.start(self.ingest)
                    self._applied["kick"] = desired_k
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one platform failing must not stop the others
            logger.exception("could not apply the stream decision for %s", platform)

    async def _log_changes(self, decisions: dict[str, Decision]) -> None:
        for platform, d in decisions.items():
            if not self.available()[platform] or self._last_logged.get(platform, "<first>") == d.target_id:
                continue
            first = platform not in self._last_logged
            self._last_logged[platform] = d.target_id
            target = self.config.target(d.target_id)
            text = f"{platform}: in '{target.label}' ({d.reason})" if target else f"{platform}: no stream ({d.reason})"
            logger.info("Streams %s", text)
            if self._log_event and not (first and target is not None and target.kind == "own"):
                task = asyncio.ensure_future(self._log_event("streams", text))
                self._bg.add(task)
                task.add_done_callback(self._bg.discard)

    async def flush(self) -> None:
        """Wait for connection-log writes that are still in flight."""
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)

    # ------------------------------------------------------------------ editing (the API calls these)
    async def add_target(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise StreamsError([{"loc": ["body"], "msg": "must be an object"}])
        platform = str(body.get("platform", "")).lower()
        if platform not in PLATFORMS:
            raise StreamsError([{"loc": ["platform"], "msg": f"must be one of {list(PLATFORMS)}"}])
        if platform == "kick":
            raise StreamsError([{"loc": ["platform"], "msg": (
                "Kick only lets the bot read chat on the channel of the account it is signed in with, and that channel "
                "is already on the list. Other Kick channels cannot be added.")}])
        if not self.available()[platform]:
            raise StreamsError([{"loc": ["platform"], "msg": "YouTube is not enabled (see docs/phase3-youtube-setup.md)"}])
        if len(self.config.targets) >= MAX_TARGETS:
            raise StreamsError([{"loc": ["body"], "msg": f"the list is full ({MAX_TARGETS} streams)"}])
        kind, ref = parse_youtube_ref(str(body.get("ref", "")))
        if any(t.platform == platform and t.kind == kind and t.ref == ref for t in self.config.targets):
            raise StreamsError([{"loc": ["ref"], "msg": "that stream is already on the list"}])
        priority = self._priority(body.get("priority", 50))
        label = clean_label(str(body.get("label", ""))) or default_label(platform, kind, ref)
        target = StreamTarget(new_target_id(platform, kind, {t.id for t in self.config.targets}), platform, kind, ref,
                              label, priority, bool(body.get("enabled", True)))
        self.config.targets.append(target)
        self.states[target.id] = TargetState(offline_since=self._clock())
        await self._persist()
        self.wake()  # look at it right away
        return target.to_dict()

    @staticmethod
    def _priority(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise StreamsError([{"loc": ["priority"], "msg": "must be a whole number from 0 to 100"}])
        return value

    async def update_target(self, target_id: str, body: Any) -> dict[str, Any]:
        target = self.config.target(target_id)
        if target is None:
            raise KeyError(target_id)
        if not isinstance(body, dict):
            raise StreamsError([{"loc": ["body"], "msg": "must be an object"}])
        unknown = [k for k in body if k not in ("label", "priority", "enabled")]
        if unknown:
            raise StreamsError([{"loc": [unknown[0]], "msg": "unknown setting (you can change label, priority, enabled)"}])
        changes: dict[str, Any] = {}
        if "label" in body:
            changes["label"] = clean_label(str(body["label"])) or default_label(target.platform, target.kind, target.ref)
        if "priority" in body:
            changes["priority"] = self._priority(body["priority"])
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise StreamsError([{"loc": ["enabled"], "msg": "must be true or false"}])
            changes["enabled"] = body["enabled"]
        updated = StreamTarget(**{**target.to_dict(), **changes})
        self.config.targets[self.config.targets.index(target)] = updated
        if not updated.enabled and self.config.pinned == updated.id:
            self.config.pinned = None  # a stream that is switched off cannot stay selected
        if changes.get("enabled") is True:
            self.states[updated.id] = TargetState(offline_since=self._clock())
        await self._persist()
        await self.reevaluate()
        self.wake()
        return updated.to_dict()

    async def remove_target(self, target_id: str) -> None:
        target = self.config.target(target_id)
        if target is None:
            raise KeyError(target_id)
        if target.kind == "own":
            raise StreamsError([{"loc": ["id"], "msg": "the signed-in account's own stream cannot be removed: switch it off instead"}])
        self.config.targets.remove(target)
        self.states.pop(target_id, None)
        self._last_search.pop(target_id, None)
        if self.config.pinned == target_id:
            self.config.pinned = None
        await self._persist()
        await self.reevaluate()

    async def select(self, target_id: str | None) -> None:
        """Choose the stream the bot must be in (None = no selection: fully automatic)."""
        if target_id is not None:
            target = self.config.target(target_id)
            if target is None:
                raise KeyError(target_id)
            if not target.enabled:
                raise StreamsError([{"loc": ["target_id"], "msg": "that stream is switched off: switch it on first"}])
        self.config.pinned = target_id
        await self._persist()
        await self.reevaluate()
        self.wake()

    async def update_settings(self, body: Any) -> dict[str, Any]:
        new = self.config.validated_settings(body)
        for key, value in new.items():
            setattr(self.config, key, value)
        await self._persist()
        await self.reevaluate()
        self.wake()
        return self.snapshot()

    # ------------------------------------------------------------------ what the dashboard shows
    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        cfg = self.config
        active_ids = {d.target_id for d in self.decisions.values() if d.target_id}
        targets = []
        for t in cfg.targets:
            st = self.states.get(t.id, TargetState())
            if not t.enabled:
                live: bool | None = None
            elif st.live:
                live = True
            else:  # None = not checked yet or the check failed: "unknown", not "offline"
                live = None if (st.checked_at is None or st.error is not None) else False
            targets.append({
                **t.to_dict(), "live": live,
                "title": st.title or None, "video_id": st.video_id, "error": st.error,
                "checked_s_ago": None if st.checked_at is None else round(now - st.checked_at),
                "offline_s": None if st.live or st.offline_since is None else round(now - st.offline_since),
                "selected": cfg.pinned == t.id, "active": t.id in active_ids,
                "note": ("checked rarely: YouTube search is limited (about every "
                         f"{cfg.youtube_channel_probe_s // 60} min per channel)") if t.kind == "channel" else None,
            })
        return {
            "targets": targets, "selected": cfg.pinned, "available": self.available(),
            "settings": {"auto_select": cfg.auto_select, "failover_after_s": cfg.failover_after_s,
                         "poll_interval_s": cfg.poll_interval_s, "youtube_channel_probe_s": cfg.youtube_channel_probe_s},
            "now": {p: {"target_id": d.target_id, "reason": d.reason} for p, d in self.decisions.items()
                    if self.available()[p]},
            "last_check_s_ago": None if self.last_tick_at is None else round(now - self.last_tick_at),
            "limits": {"max_streams": MAX_TARGETS, "kinds": {k: list(v) for k, v in KINDS.items()}},
        }


class _Failed(Exception):
    """A check that could not be made (quota guard, ...): the stream's state is unknown, not offline."""
