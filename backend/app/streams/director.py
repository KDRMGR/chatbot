"""The stream director: keeps the watch list, checks which streams are live, and points the platform adapters at the
right one (see ``choose.py`` for the rules). It never sends or reads chat itself.

What it does to the adapters, and only when its decision CHANGES (so a manual Connect/Disconnect in the dashboard is
not fought over):
  YouTube  ``retarget(video_id)`` moves the adapter to that live video (``None`` = its normal discovery of the signed-in
           account's stream); with no live stream and no "own" target switched on it stops polling.
  Kick     ``retarget(channel name)`` moves the adapter to that channel (``None`` = the default channel); start / stop when
           nothing is chosen at all. One call (``GET /channels?slug=...``) tells which of the listed channels are live.

When the stream the bot is "in" (the focus) changes, ``on_focus`` is called so the stream's own topic and chat memory can be
switched too.
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
    KINDS, MAX_TARGETS, OWN_IDS, OWN_LABELS, PLATFORMS, StreamsConfig, StreamsError, StreamTarget, clean_label,
    default_label, detect_platform, new_target_id, parse_kick_ref, parse_youtube_ref,
)

logger = logging.getLogger(__name__)
LogEvent = Callable[[str, str], Awaitable[None]]
SaveConfig = Callable[[dict[str, Any]], Awaitable[None]]
FocusChanged = Callable[[str, str, bool], Awaitable[None]]  # (target id, its label, is this the first focus since start)
_DEFAULT_YT = ("default",)
_DEFAULT_KICK = ("running", None)


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
        self._applied: dict[str, tuple[Any, ...]] = {"youtube": _DEFAULT_YT, "kick": _DEFAULT_KICK}
        self._kick_live: dict[str, Live] = {}  # this tick's answer of Kick's channel lookup, by channel name
        self._focus: str | None = None
        self.on_focus: FocusChanged | None = None  # set by main: the stream the bot is in changed
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
        await self._refresh_kick()
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
            await self._update_focus()

    def focus_id(self) -> str | None:
        """The stream the bot is "in" right now: the one you selected if the bot is there, else the first on the list that is
        active. Its topic and chat memory are the ones in use."""
        active = {d.target_id for d in self.decisions.values() if d.target_id}
        if self.config.pinned in active:
            return self.config.pinned
        return next((t.id for t in self.config.targets if t.id in active), None)

    async def _update_focus(self) -> None:
        focus = self.focus_id()
        if focus is None or focus == self._focus:
            return
        first = self._focus is None
        self._focus = focus
        target = self.config.target(focus)
        if self.on_focus is not None and target is not None:
            try:
                await self.on_focus(focus, self.display_label(target), first)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a topic problem must never stop the stream decisions
                logger.exception("could not switch the stream's topic")

    # ------------------------------------------------------------------ probes
    def _guard(self, method: str) -> None:
        if self._quota is not None and not self._quota.can_read(method):
            raise _Failed("YouTube quota guard: check skipped")

    async def _probe(self, t: StreamTarget, state: TargetState) -> Live:
        try:
            if t.platform == "kick":
                return self._probe_kick(t)
            return await self._probe_youtube(t, state)
        except _Skip:
            raise
        except _Failed as exc:
            return Live(None, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed check says nothing about the stream
            return Live(None, error=str(getattr(exc, "reason", None) or exc.__class__.__name__)[:80])

    def _kick_slug(self, t: StreamTarget) -> str | None:
        """The Kick channel name a target stands for (the default entry: the channel the bot starts in)."""
        if t.kind == "channel":
            return t.ref
        default = getattr(self._kick, "default_slug", None)
        return default() if callable(default) else None

    async def _refresh_kick(self) -> None:
        """One call for all the Kick channels on the list; ``_probe_kick`` then reads the answer for each of them."""
        self._kick_live = {}
        lookup = getattr(self._kick, "live_status", None)
        if not callable(lookup):
            return
        slugs: list[str] = []
        for t in self.config.targets:
            slug = self._kick_slug(t) if (t.platform == "kick" and t.enabled) else None
            if slug and slug not in slugs:
                slugs.append(slug)
        if not slugs:
            return
        try:
            rows = await lookup(slugs[:50])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed check says nothing about the streams
            error = str(getattr(exc, "detail", None) or exc.__class__.__name__)[:80]
            self._kick_live = {s: Live(None, error=error) for s in slugs}
            return
        for slug in slugs:
            row = rows.get(slug)
            self._kick_live[slug] = (Live(bool(row.get("is_live")), title=str(row.get("title") or "")) if row
                                     else Live(None, error="there is no such channel on Kick"))

    def _probe_kick(self, t: StreamTarget) -> Live:
        if self._kick is None:
            return Live(None, error="Kick is not enabled")
        status = self._kick.status()
        if status.state is ConnectionState.ERROR:
            return Live(None, error=status.detail or "Kick connection problem")
        slug = self._kick_slug(t)
        if slug and slug in self._kick_live:
            return self._kick_live[slug]
        if t.kind == "channel":
            return Live(None, error="not known yet")
        is_live = self._kick.details().get("is_live")  # the default channel, while the adapter is in it
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
                target = self.config.target(d.target_id)
                desired_k: tuple[Any, ...] = (("stopped",) if target is None
                                              else ("running", target.ref if target.kind == "channel" else None))
                if desired_k != self._applied["kick"]:
                    previous_k = self._applied["kick"]
                    if desired_k[0] == "stopped":
                        await self._kick.stop()
                    else:
                        retarget = getattr(self._kick, "retarget", None)
                        if callable(retarget) and (desired_k[1] != previous_k[1] if previous_k[0] == "running" else True):
                            await retarget(desired_k[1])  # remembers the channel; reconnects if the adapter is running
                        if previous_k[0] == "stopped" and self.ingest is not None:
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
            text = (f"{platform}: in '{self.display_label(target)}' ({d.reason})" if target
                    else f"{platform}: no stream ({d.reason})")
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
        text = str(body.get("ref", ""))
        platform = str(body.get("platform") or "").lower() or (detect_platform(text) or "")
        if not platform:
            raise StreamsError([{"loc": ["ref"], "msg": "paste a full YouTube or Kick link (for example kick.com/name)"}])
        if platform not in PLATFORMS:
            raise StreamsError([{"loc": ["platform"], "msg": f"must be one of {list(PLATFORMS)}"}])
        if not self.available()[platform]:
            raise StreamsError([{"loc": ["platform"], "msg": (
                "YouTube is not enabled (see docs/phase3-youtube-setup.md)" if platform == "youtube"
                else "Kick is not enabled (STEALTHBOT_KICK_ENABLED)")}])
        if len(self.config.targets) >= MAX_TARGETS:
            raise StreamsError([{"loc": ["body"], "msg": f"the list is full ({MAX_TARGETS} streams)"}])
        if platform == "kick":
            kind, ref = "channel", parse_kick_ref(text)
            if ref == self._kick_slug(StreamTarget(OWN_IDS["kick"], "kick", "own")):
                raise StreamsError([{"loc": ["ref"], "msg": "that is the default Kick channel, which is already on the list"}])
        else:
            kind, ref = parse_youtube_ref(text)
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
    def display_label(self, t: StreamTarget) -> str:
        """The name shown to people. The default Kick entry is named after the channel it stands for, not "the signed-in
        account's channel" (which is not who the viewers see)."""
        if t.platform == "kick" and t.kind == "own" and t.label == OWN_LABELS["kick"]:
            slug = self._kick_slug(t)
            return f"Kick: {slug}" if slug else t.label
        return t.label

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
                **t.to_dict(), "label": self.display_label(t), "live": live,
                "title": st.title or None, "video_id": st.video_id, "error": st.error,
                "checked_s_ago": None if st.checked_at is None else round(now - st.checked_at),
                "offline_s": None if st.live or st.offline_since is None else round(now - st.offline_since),
                "selected": cfg.pinned == t.id, "active": t.id in active_ids,
                "note": ("checked rarely: YouTube search is limited (about every "
                         f"{cfg.youtube_channel_probe_s // 60} min per channel)") if (t.kind == "channel" and t.platform == "youtube") else None,
            })
        focus = cfg.target(self.focus_id())
        return {
            "targets": targets, "selected": cfg.pinned, "available": self.available(),
            "focus": {"id": focus.id, "label": self.display_label(focus)} if focus else None,
            "settings": {"auto_select": cfg.auto_select, "failover_after_s": cfg.failover_after_s,
                         "poll_interval_s": cfg.poll_interval_s, "youtube_channel_probe_s": cfg.youtube_channel_probe_s},
            "now": {p: {"target_id": d.target_id, "reason": d.reason} for p, d in self.decisions.items()
                    if self.available()[p]},
            "last_check_s_ago": None if self.last_tick_at is None else round(now - self.last_tick_at),
            "limits": {"max_streams": MAX_TARGETS, "kinds": {k: list(v) for k, v in KINDS.items()}},
        }


class _Failed(Exception):
    """A check that could not be made (quota guard, ...): the stream's state is unknown, not offline."""
