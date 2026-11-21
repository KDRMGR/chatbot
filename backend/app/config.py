"""Central configuration. Plain dataclasses + a small environment loader (no Pydantic: see docs/phase2-core-engine.md,
"Smart App Control": this PC blocks unsigned native extensions, and pydantic-core is one).

Every value can be overridden with a ``STEALTHBOT_<FIELD>`` environment variable or in ``backend/.env``.
Runtime state (database, logs, API token) lives OUTSIDE the project folder by default, in
``%LOCALAPPDATA%\\stealthbot``, because the project sits in a OneDrive-synced folder and a live SQLite
database plus rotating log files must not be synced.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from types import UnionType
from typing import Any, Mapping, Union, get_args, get_origin, get_type_hints
from urllib.parse import urlparse

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
ENV_PREFIX = "STEALTHBOT_"


def default_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home()) / "stealthbot"


@dataclass
class RateLimitConfig:
    """Per-platform outgoing safeguards. Initial values are conservative defaults, NOT platform guarantees."""

    min_gap_s: float = 8.0  # minimum seconds between any two bot messages
    max_per_window: int = 15  # at most this many bot messages per window
    window_s: float = 600.0
    user_cooldown_s: float = 45.0  # don't reply to the same viewer more often than this
    max_chars: int = 200  # platform message length; verify against official docs in Phase 3/4


def _default_rate_limits() -> dict[str, RateLimitConfig]:
    return {
        # YouTube: an insert costs 20 of the 10,000 daily quota units, and chat reading costs quota too (see
        # platforms/youtube/polling.py), so ~7 messages per 10 minutes keeps a 6-hour stream inside the default quota.
        # max_chars=200 is the commonly known live-chat limit; Google's API docs do NOT state a limit (verified absent).
        "youtube": RateLimitConfig(max_per_window=7, max_chars=200),
        "kick": RateLimitConfig(max_chars=500),
        # The simulator is for development: no throttling so conversations feel interactive.
        "simulator": RateLimitConfig(min_gap_s=0, max_per_window=10_000, user_cooldown_s=0, max_chars=500),
    }


# Webhook events the Kick connector understands (names from Kick's docs, events/event-types.md). Chat is the default; the others
# make the bot thank new subscribers and KICKs gifts the same way it thanks YouTube memberships and Super Chats.
KICK_SUPPORTED_EVENTS = (
    "chat.message.sent", "channel.subscription.new", "channel.subscription.renewal", "channel.subscription.gifts", "kicks.gifted",
)


def _default_commands() -> list[str]:
    return ["!bot", "!about", "!commands", "!game"]


@dataclass
class Settings:
    # --- identity / stream ---
    app_name: str = "StealthBot"
    bot_display_name: str = "StealthBot"
    streamer_name: str = "StealthModeGG"
    current_game: str = "GTA 6"

    # --- server (loopback only) ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- state directories (outside OneDrive by default) ---
    state_dir: Path = field(default_factory=default_state_dir)
    data_dir: Path | None = None
    log_dir: Path | None = None
    log_level: str = "INFO"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5
    extra_lexicon_file: Path | None = None  # optional extra regex terms, one per line

    # --- Ollama / models ---
    ollama_host: str = "http://127.0.0.1:11434"
    allow_remote_ollama: bool = False  # keep False: Ollama must never leave this machine
    primary_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    fallback_model: str | None = "qwen3:1.7b"
    ai_enabled: bool = True
    num_ctx: int = 4096
    num_predict: int = 80
    temperature: float = 0.8
    num_gpu: int | None = 99  # 99 = all layers on GPU (measured in Phase 1); None = let Ollama decide
    keep_alive: str = "30m"
    generation_timeout_s: float = 30.0  # per attempt, warm model
    generation_deadline_s: float = 60.0  # whole generate() call incl. retry and fallback
    warmup_timeout_s: float = 180.0  # first load after install / idle can take 30-60 s
    model_recheck_interval_s: float = 60.0  # how long a failed primary is skipped
    model_switch_timeout_s: float = 90.0  # loading + testing a model chosen in the dashboard (chat waits meanwhile)
    health_interval_s: float = 30.0
    stats_flush_interval_s: float = 300.0  # how often session statistics are saved while running (not just at a clean stop)
    gc_interval_s: float = 60.0  # full garbage collection this often (keeps memory flat over hours); 0 turns it off

    # --- reply shaping ---
    reply_target_min_words: int = 10  # what the prompt asks the model for (spec: 10-35 words)
    reply_min_words: int = 3  # the guard only rejects replies shorter than this
    reply_max_words: int = 35
    similarity_threshold: float = 0.85
    recent_reply_memory: int = 20
    response_cache_ttl_s: float = 600.0

    # --- chat intake ---
    incoming_queue_max: int = 200
    max_queue_age_s: float = 120.0  # a viewer message that waited longer than this is kept in history but not answered
    approval_required: bool = False  # start with "ask me before sending" on (also switchable in the dashboard)
    approval_timeout_s: float = 120.0  # a message nobody approves within this long is dropped
    approval_queue_max: int = 50
    submit_timeout_s: float = 90.0
    allowed_commands: list[str] = field(default_factory=_default_commands)
    flood_window_s: float = 10.0
    flood_max_messages: int = 5
    repeat_window_s: float = 120.0
    copypasta_window_s: float = 60.0
    copypasta_min_users: int = 3
    context_chat_lines: int = 6
    context_max_chars: int = 700

    rate_limits: dict[str, RateLimitConfig] = field(default_factory=_default_rate_limits)

    # --- YouTube (Phase 3). Off by default: needs scripts\youtube_login.py first ---
    youtube_enabled: bool = False
    youtube_video_id: str | None = None  # the live stream's video id (most reliable: paste it for each stream)
    youtube_live_chat_id: str | None = None  # advanced: skip discovery entirely
    youtube_streamer_channel_id: str | None = None  # find the streamer's live video automatically (search.list)
    youtube_daily_quota: int = 10_000  # Google's default; raise only if Google granted you more
    youtube_use_streaming: bool = False  # EXPERIMENTAL HTTP streaming transport (framing undocumented); polling is the default
    youtube_skip_backlog: bool = True  # never answer messages that were already in chat before we connected
    youtube_discovery_interval_s: float = 60.0
    youtube_poll_active_s: float = 10.0  # poll pace when chat is active ...
    youtube_poll_idle_s: float = 30.0  # ... a few minutes quiet ...
    youtube_poll_quiet_s: float = 60.0  # ... and nobody around (budget pacing may slow these further)
    youtube_planned_stream_hours: float = 6.0
    youtube_send_budget_fraction: float = 0.40  # share of the daily quota that chat reading may never use (it is for sending)
    # Google's developer policy: stored API data (channel ids, display names, messages) must be deleted or refreshed
    # after 30 days. The app deletes YouTube-derived records older than this. 30 is the maximum allowed.
    youtube_data_retention_days: int = 30

    # --- Kick (Phase 4). Off by default: needs scripts\kick_login.py and a tunnel to the webhook port (docs/phase4-kick-setup.md) ---
    kick_enabled: bool = False
    kick_webhook_port: int = 8787  # loopback only; the tunnel program (ngrok, Cloudflare Tunnel) forwards Kick's calls here
    kick_oauth_port: int = 8789  # the sign-in redirect http://localhost:<port>/callback; register exactly this in the Kick app
    kick_webhook_public_url: str | None = None  # the public URL you typed into Kick's settings; shown in the dashboard only
    kick_message_type: str = "bot"  # "bot" = posts under the app's bot identity (documented); "user" = posts as the signed-in account
    kick_reply_threads: bool = False  # answer as a threaded reply (reply_to_message_id); off until seen working live
    kick_events: list[str] = field(default_factory=lambda: ["chat.message.sent"])  # more: see KICK_SUPPORTED_EVENTS
    kick_bot_user_id: str | None = None  # if known: messages from this user id are our own and ignored
    kick_webhook_tolerance_s: float = 600.0  # deliveries older/newer than this are refused (replay protection)
    kick_resubscribe_interval_s: float = 300.0  # how often the subscription is checked (Kick drops failing ones after a day)

    # --- Proactive engagement (Phase 5). Spec starting values: general 8-12 min, gaming question 15-20 min, trivia 30-40 min,
    # quiet-chat detection 5-7 min. These are configurable starting points, not platform guarantees.
    proactive_enabled: bool = True  # the streamer can also switch it off live in the dashboard
    proactive_platforms: list[str] = field(default_factory=lambda: ["youtube", "kick"])  # never the simulator unless listed
    proactive_tick_s: float = 5.0  # how often the scheduler looks at the clock
    proactive_quiet_after_s: float = 360.0  # nobody wrote for this long = chat is quiet (6 min)
    proactive_min_gap_s: float = 240.0  # at least this long since ANY bot message before a proactive one
    proactive_platform_stagger_s: float = 45.0  # a proactive message on one platform holds the others back this long
    proactive_viewer_reserve: float = 0.4  # share of a platform's message allowance proactive messages may never use
    proactive_retry_s: float = 30.0  # wait after a skipped attempt before trying again
    proactive_general_min_s: float = 480.0
    proactive_general_max_s: float = 720.0
    proactive_question_min_s: float = 900.0
    proactive_question_max_s: float = 1200.0
    proactive_trivia_min_s: float = 1800.0
    proactive_trivia_max_s: float = 2400.0

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        if self.data_dir is None:
            self.data_dir = self.state_dir / "data"
        if self.log_dir is None:
            self.log_dir = self.state_dir / "logs"
        self.data_dir, self.log_dir = Path(self.data_dir), Path(self.log_dir)
        self.rate_limits = {
            k: v if isinstance(v, RateLimitConfig) else RateLimitConfig(**v) for k, v in self.rate_limits.items()
        }

        host = urlparse(self.ollama_host).hostname or ""
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback and not self.allow_remote_ollama:
            raise ValueError(
                f"ollama_host {self.ollama_host!r} is not a loopback address. Ollama must stay local; "
                "set STEALTHBOT_ALLOW_REMOTE_OLLAMA=true only if you really mean it."
            )
        if self.host not in ("127.0.0.1", "localhost"):
            raise ValueError("The dashboard/API must bind to loopback (127.0.0.1) only.")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if self.num_ctx < 512 or self.num_predict < 1:
            raise ValueError("num_ctx must be >= 512 and num_predict >= 1")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if not 1 <= self.youtube_data_retention_days <= 30:
            raise ValueError("youtube_data_retention_days must be between 1 and 30 (YouTube developer policy maximum)")
        if not 0.0 <= self.youtube_send_budget_fraction <= 0.8:
            raise ValueError("youtube_send_budget_fraction must be between 0 and 0.8")
        if min(self.youtube_poll_active_s, self.youtube_poll_idle_s, self.youtube_poll_quiet_s,
               self.youtube_discovery_interval_s, self.youtube_planned_stream_hours) <= 0 or self.youtube_daily_quota < 1:
            raise ValueError("YouTube poll intervals, planned hours and quota must be positive")
        if self.kick_message_type not in ("bot", "user"):
            raise ValueError("kick_message_type must be 'bot' or 'user'")
        unknown = [e for e in self.kick_events if e not in KICK_SUPPORTED_EVENTS]
        if unknown or not self.kick_events:
            raise ValueError(f"kick_events must be a non-empty subset of {sorted(KICK_SUPPORTED_EVENTS)} (got {unknown or 'none'})")
        if not 0 <= self.kick_webhook_port <= 65535 or not 1 <= self.kick_oauth_port <= 65535:
            raise ValueError("kick ports must be between 1 and 65535")
        if self.kick_webhook_port in (self.port, self.kick_oauth_port) and self.kick_webhook_port != 0:
            raise ValueError("the Kick webhook port must differ from the dashboard port and the Kick sign-in port: the internet-facing "
                             "webhook listener must never share a port with the control panel")
        if self.kick_webhook_tolerance_s <= 0 or self.kick_resubscribe_interval_s <= 0:
            raise ValueError("kick_webhook_tolerance_s and kick_resubscribe_interval_s must be positive")
        if self.stats_flush_interval_s <= 0 or self.max_queue_age_s <= 0:
            raise ValueError("stats_flush_interval_s and max_queue_age_s must be positive")
        if self.approval_timeout_s <= 0 or self.approval_queue_max < 1:
            raise ValueError("approval_timeout_s must be positive and approval_queue_max at least 1")
        if min(self.proactive_tick_s, self.proactive_retry_s) <= 0:
            raise ValueError("proactive_tick_s and proactive_retry_s must be positive")
        if min(self.proactive_quiet_after_s, self.proactive_min_gap_s, self.proactive_platform_stagger_s) < 0:
            raise ValueError("proactive quiet/gap/stagger times cannot be negative")
        if not 0.0 <= self.proactive_viewer_reserve < 1.0:
            raise ValueError("proactive_viewer_reserve must be between 0 and 1 (exclusive)")
        for name in ("general", "question", "trivia"):
            lo, hi = getattr(self, f"proactive_{name}_min_s"), getattr(self, f"proactive_{name}_max_s")
            if not 0 < lo <= hi:
                raise ValueError(f"proactive_{name}_min_s must be positive and not larger than proactive_{name}_max_s")

    # --- derived paths ---
    @property
    def database_path(self) -> Path:
        assert self.data_dir is not None
        return self.data_dir / "stealthbot.db"

    @property
    def api_token_file(self) -> Path:
        assert self.data_dir is not None
        return self.data_dir / "api_token"

    def limits_for(self, platform: str) -> RateLimitConfig:
        return self.rate_limits.get(platform) or RateLimitConfig()

    def ensure_dirs(self) -> None:
        assert self.data_dir is not None and self.log_dir is not None
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------- environment loading
def _coerce(tp: Any, raw: str) -> Any:
    origin = get_origin(tp)
    if origin in (Union, UnionType):
        if raw.strip().lower() in ("", "none", "null"):
            return None
        return _coerce([a for a in get_args(tp) if a is not type(None)][0], raw)
    if tp is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if tp is int:
        return int(raw)
    if tp is float:
        return float(raw)
    if tp is Path:
        return Path(raw)
    if origin is list:
        text = raw.strip()
        return json.loads(text) if text.startswith("[") else [s.strip() for s in text.split(",") if s.strip()]
    if origin is dict:  # rate_limits: JSON like {"youtube": {"min_gap_s": 10}}
        defaults = _default_rate_limits()
        merged = dict(defaults)
        for name, values in json.loads(raw).items():
            base = defaults.get(name, RateLimitConfig())
            merged[name] = RateLimitConfig(**{**base.__dict__, **values})
        return merged
    return raw


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().upper()] = _env_value(value)
    return values


def _env_value(raw: str) -> str:
    """A quoted value is taken as written; an unquoted one ends at ' #' (an inline comment, as .env.example uses them)."""
    value = raw.strip()
    if value[:1] in ("\"", "'"):
        end = value.find(value[0], 1)
        return value[1:end] if end != -1 else value[1:]
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1].isspace():
            return value[:i].strip()
    return value


def load_settings(
    environ: Mapping[str, str] | None = None, env_file: Path | None = BACKEND_DIR / ".env", **overrides: Any
) -> Settings:
    """Defaults < backend/.env < real environment variables < explicit keyword overrides."""
    merged = {**(_read_env_file(env_file) if env_file else {}), **{k.upper(): v for k, v in (environ or os.environ).items()}}
    hints = get_type_hints(Settings)
    values: dict[str, Any] = {}
    for f in fields(Settings):
        key = ENV_PREFIX + f.name.upper()
        if key in merged:
            try:
                values[f.name] = _coerce(hints[f.name], merged[key])
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f"Invalid value for {key}: {exc}") from exc
    values.update(overrides)
    return Settings(**values)


@lru_cache
def get_settings() -> Settings:
    return load_settings()
