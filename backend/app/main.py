"""Application factory and lifecycle. Run with:  uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx
from sqlalchemy.engine import Engine
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai.model_manager import ModelManager
from .ai.ollama_client import OllamaClient
from .ai.prompt_manager import PromptManager
from .ai.response_generator import ResponseGenerator
from .api import routes
from .api.schemas import ValidationFailed
from .api import dashboard
from .api.security import TicketStore, load_or_create_token
from .chat.context import SessionContext
from .chat.filters import MessageFilter
from .chat.lexicon import build_lexicon
from .chat.models import Platform
from .chat.output_guard import RecentReplies
from .chat.processor import ChatProcessor
from .chat.rules import RuleEngine
from .config import REPLY_LANGUAGES, Settings, get_settings
from .database.database import make_engine, make_session_factory, run_migrations
from .database.repository import Repository
from .logging_setup import setup_logging
from .personalities.defaults import default_personality_rows
from .personalities.manager import PersonalityManager, PersonalityProfile
from .platforms.base import ConnectionState, PlatformAdapter
from .platforms.simulator import SimulatorAdapter
from .platforms.kick.adapter import KickAdapter
from .platforms.kick.api import KickApi
from .platforms.kick.credentials import KickAuth
from .platforms.youtube.adapter import YouTubeAdapter
from .platforms.youtube.api import YouTubeApi
from .platforms.youtube.credentials import YouTubeAuth
from .platforms.youtube.quota import QuotaTracker
from .scheduler.engagement import EngagementScheduler
from .streams.director import StreamDirector
from .knowledge.topic import TopicBriefing
from .knowledge.research import Researcher
from .knowledge.social import REDDIT_KEY, SocialBuzz
from .learning.service import LearningService
from .vision.obs import CREDENTIAL_KEY as OBS_KEY, ObsClient
from .vision.service import VisionService
from .relay.service import RelayService
from .relay.voice import make_voice_handler
from .relay.supabase import SupabaseClient
from .security.credential_store import CredentialStore
from .services.controls import RuntimeControls
from .services.maintenance import run_gc_loop
from .services.runtime_settings import AI_FIELDS, RATE_FIELDS, SettingsError, apply_ai, apply_rate_limits, parse_numbers
from .services.system_stats import SystemStats
from .services.retention import RetentionService
from .services.session_manager import SessionManager
from .services.statistics import Metrics
from .chat.normalizer import now_utc

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: Engine
    repo: Repository
    ollama: OllamaClient
    model_manager: ModelManager
    generator: ResponseGenerator
    processor: ChatProcessor
    session_manager: SessionManager
    metrics: Metrics
    controls: RuntimeControls
    context: SessionContext
    personalities: PersonalityManager
    adapters: dict[Platform, PlatformAdapter]
    api_token: str = field(repr=False)  # never printed or logged
    youtube: Any = None  # YouTubeAdapter when enabled
    tickets: TicketStore = field(default_factory=TicketStore, repr=False)  # one-time dashboard sign-in tickets
    scheduler: Any = None  # EngagementScheduler (proactive messages)
    system_stats: Any = None  # SystemStats (CPU / RAM / GPU readings)
    ingest: Any = None  # what platform adapters call for every incoming message (the dashboard's Connect button needs it)
    streams: Any = None  # StreamDirector (which stream the bot comments in)
    topic: Any = None  # TopicBriefing (what the stream is about: game name + a short background note)
    relay: Any = None  # RelayService (online panel through Supabase), when enabled
    vision: Any = None  # VisionService (the bot's eyes: a one-sentence description of the stream's picture)
    learning: Any = None  # LearningService (stream notes, viewer notes, the streamer's thumbs, what gets chat talking)
    researcher: Any = None  # Researcher (looks up a viewer question's key words before answering)
    social: Any = None  # SocialBuzz (what people online are saying about the topic)


def create_app(
    settings: Settings | None = None, *, ollama_transport: httpx.AsyncBaseTransport | None = None,
    youtube_transport: httpx.AsyncBaseTransport | None = None, kick_transport: httpx.AsyncBaseTransport | None = None,
    topic_transport: httpx.AsyncBaseTransport | None = None, relay_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        cfg.ensure_dirs()
        assert cfg.log_dir is not None
        setup_logging(cfg.log_dir, cfg.log_level, cfg.log_max_bytes, cfg.log_backup_count)
        logger.info("Starting %s (configured primary=%s, fallback=%s)", cfg.app_name, cfg.primary_model, cfg.fallback_model)

        await run_migrations(cfg.database_path)
        engine = make_engine(cfg.database_path)
        repo = Repository(make_session_factory(engine))
        await repo.seed_personalities(default_personality_rows(), now_utc())
        retention = RetentionService(repo, cfg)
        await retention.run_once()  # YouTube policy: drop stored API data older than the retention limit

        metrics = Metrics()
        controls = RuntimeControls(ai_enabled=cfg.ai_enabled, proactive_enabled=cfg.proactive_enabled,
                                   approval_required=cfg.approval_required)
        # Settings edited in the dashboard earlier win over the defaults and over .env until they are changed again.
        for key, apply in (("ai.params", lambda d: apply_ai(cfg, parse_numbers(d, AI_FIELDS))),
                           ("rate_limits", lambda d: [apply_rate_limits(cfg, p, parse_numbers(v, RATE_FIELDS))
                                                      for p, v in d.items() if p in cfg.rate_limits])):
            raw = await repo.get_setting(key)
            if raw:
                try:
                    apply(json.loads(raw))
                except (ValueError, TypeError, SettingsError):
                    logger.warning("ignoring invalid saved setting %s", key)
        saved_language = await repo.get_setting("reply.language")  # chosen in the panel earlier
        if saved_language:
            try:
                value = json.loads(saved_language)
                if value in REPLY_LANGUAGES:
                    cfg.reply_language = value
            except (ValueError, TypeError):
                logger.warning("ignoring an unreadable saved reply language")
        session_manager = SessionManager(repo, cfg, metrics)
        await session_manager.start()

        lexicon = build_lexicon(cfg.extra_lexicon_file)
        context = SessionContext(
            cfg.streamer_name, cfg.current_game, started_at=session_manager.started_at, max_chat=cfg.context_chat_lines
        )
        async def save_topic(state: dict[str, Any]) -> None:
            await repo.set_setting("topic.state", json.dumps(state), now_utc())

        topic = TopicBriefing(cfg, context, transport=topic_transport, save=save_topic)
        await topic.load(await repo.get_setting("topic.state"))  # the game and background note saved from the dashboard earlier
        recent = RecentReplies(cfg.recent_reply_memory)
        personalities = PersonalityManager(PersonalityProfile.from_row(r) for r in await repo.list_personalities())
        ollama = OllamaClient(cfg.ollama_host, transport=ollama_transport)
        model_manager = ModelManager(ollama, cfg)
        saved_primary = await repo.get_setting("ai.primary_model")  # chosen in the dashboard earlier
        saved_fallback = await repo.get_setting("ai.fallback_model")  # "" = deliberately no fallback
        if saved_primary or saved_fallback is not None:
            await model_manager.restore(
                saved_primary, saved_fallback or None, have_fallback_choice=saved_fallback is not None)
        generator = ResponseGenerator(cfg, model_manager, PromptManager(cfg), lexicon, recent, context, metrics)
        adapters: dict[Platform, PlatformAdapter] = {Platform.SIMULATOR: SimulatorAdapter()}

        youtube_http: httpx.AsyncClient | None = None
        youtube_api: YouTubeApi | None = None
        youtube: YouTubeAdapter | None = None
        quota: QuotaTracker | None = None
        persist_quota: Any = None
        if cfg.youtube_enabled:
            youtube_http = httpx.AsyncClient(transport=youtube_transport, trust_env=False, timeout=20.0)
            quota = QuotaTracker(cfg.youtube_daily_quota)
            try:
                saved = await repo.get_setting("youtube.quota")
                if saved:
                    quota.load(json.loads(saved))  # keep today's count across restarts
            except ValueError:
                logger.warning("ignoring unreadable saved YouTube quota state")
            assert cfg.data_dir is not None
            youtube_api = YouTubeApi(YouTubeAuth(CredentialStore(cfg.data_dir), youtube_http), quota,
                                     transport=youtube_transport)

            async def log_youtube(kind: str, message: str) -> None:
                await repo.log_connection("youtube", kind, message, now_utc())

            async def save_quota(state: dict[str, Any]) -> None:
                await repo.set_setting("youtube.quota", json.dumps(state), now_utc())

            youtube = YouTubeAdapter(cfg, youtube_api, quota, log_event=log_youtube, save_quota=save_quota)
            adapters[Platform.YOUTUBE] = youtube

            async def persist_quota() -> None:  # the stream director's checks use quota too
                await save_quota(quota.dump())

        kick_http: httpx.AsyncClient | None = None
        kick_api: KickApi | None = None
        if cfg.kick_enabled:
            assert cfg.data_dir is not None
            kick_http = httpx.AsyncClient(transport=kick_transport, trust_env=False, timeout=20.0)
            kick_auth = KickAuth(CredentialStore(cfg.data_dir), kick_http)
            kick_api = KickApi(kick_auth, kick_http)

            async def log_kick(kind: str, message: str) -> None:
                await repo.log_connection("kick", kind, message, now_utc())

            adapters[Platform.KICK] = KickAdapter(cfg, kick_api, kick_auth, log_event=log_kick)

        processor = ChatProcessor(
            settings=cfg, repo=repo, msg_filter=MessageFilter(cfg, lexicon), personalities=personalities,
            generator=generator, rules=RuleEngine(cfg, context, lexicon), adapters=adapters, context=context,
            recent=recent, metrics=metrics, controls=controls, session_id=lambda: session_manager.session_id,
        )
        async def save_scheduler(settings_dict: dict[str, Any]) -> None:
            await repo.set_setting("scheduler.settings", json.dumps(settings_dict), now_utc())

        scheduler = EngagementScheduler(cfg, processor, controls, personalities, adapters, save=save_scheduler)
        saved_scheduler = await repo.get_setting("scheduler.settings")  # edited in the dashboard earlier
        if saved_scheduler:
            try:
                scheduler.restore(json.loads(saved_scheduler))
            except ValueError:
                logger.warning("ignoring unreadable saved scheduler settings")
        processor.activity = scheduler  # the pipeline tells it when viewers and the bot speak

        # ---- the eyes, what the bot learns, and looking things up (each can be switched off in the panel)
        async def save_vision(state: dict[str, Any]) -> None:
            await repo.set_setting("vision.v1", json.dumps(state), now_utc())

        async def save_learning(state: dict[str, Any]) -> None:
            await repo.set_setting("learning.v1", json.dumps(state), now_utc())

        def obs_password() -> str | None:
            assert cfg.data_dir is not None
            return str((CredentialStore(cfg.data_dir).load(OBS_KEY) or {}).get("password") or "") or None

        def streaming_now() -> bool:
            return any(a.status().state is ConnectionState.CONNECTED for p, a in adapters.items() if p in (Platform.KICK, Platform.YOUTUBE))

        def chat_is_idle() -> bool:
            return processor.incoming.qsize() == 0 and not controls.outgoing_paused

        vision = VisionService(
            cfg, model_manager, context, lexicon, obs=ObsClient(cfg.obs_host, cfg.obs_port, obs_password), save=save_vision,
            should_look=lambda: controls.ai_enabled and cfg.ai_enabled and chat_is_idle() and streaming_now())
        learning = LearningService(cfg, model_manager, context, lexicon, save=save_learning, idle=chat_is_idle, vision=vision)
        researcher = Researcher(cfg, topic, context)

        def reddit_credentials() -> dict[str, Any] | None:
            assert cfg.data_dir is not None
            return CredentialStore(cfg.data_dir).load(REDDIT_KEY)

        social = SocialBuzz(cfg, topic, context, credentials=reddit_credentials)
        for key, restore in (("vision.v1", lambda raw: vision.restore(json.loads(raw))), ("learning.v1", learning.restore),
                             ("features.v1", lambda raw: setattr(researcher, "enabled", bool(json.loads(raw).get("research", researcher.enabled))))):
            saved_raw = await repo.get_setting(key)
            if saved_raw:
                try:
                    restore(saved_raw)
                except (ValueError, TypeError, AttributeError):
                    logger.warning("ignoring unreadable saved setting %s", key)
        processor.learning, processor.researcher = learning, researcher
        processor.on_viewer_arrival = model_manager.interrupt_look
        personalities.weight_of = learning.weight
        scheduler.screen_ready, scheduler.screen_spoken = vision.has_change, vision.consume_change
        scheduler.buzz_ready, scheduler.buzz_spoken = social.has_new, social.consume

        def channel_is_live(platform: Platform) -> bool | None:
            details = getattr(adapters.get(platform), "details", None)
            return details().get("is_live") if callable(details) else None

        scheduler.is_live = channel_is_live  # unprompted messages only go to a channel that is live

        async def save_streams(config_dict: dict[str, Any]) -> None:
            await repo.set_setting("streams.config", json.dumps(config_dict), now_utc())

        async def log_streams(kind: str, message: str) -> None:
            await repo.log_connection("streams", kind, message, now_utc())

        director = StreamDirector(
            youtube=youtube, youtube_api=youtube_api, quota=quota, kick=adapters.get(Platform.KICK), save=save_streams,
            log_event=log_streams, persist_quota=persist_quota, first_delay_s=cfg.streams_first_check_delay_s)
        await director.load(await repo.get_setting("streams.config"))  # the watch list and the selection, saved earlier

        async def on_focus(target_id: str, label: str, first: bool) -> None:
            """The bot is now in another stream: that stream's own topic and headlines replace the previous one's, and the
            previous stream's chat is forgotten (it has nothing to do with this one)."""
            changed = await topic.switch_to(target_id, label)
            if changed and not first:
                context.reset_chat()
                logger.info("Topic switched with the stream to '%s' (%s)", label, context.game_label)
            learning.on_focus(target_id)  # the stream's own notes come with it
            if changed:
                social.clear()  # what people said about the previous stream does not apply here
            social.wake()

        director.on_focus = on_focus
        app.state.container = Container(
            settings=cfg, engine=engine, repo=repo, ollama=ollama, model_manager=model_manager,
            generator=generator, processor=processor, session_manager=session_manager, metrics=metrics,
            controls=controls, context=context, personalities=personalities, adapters=adapters,
            api_token=load_or_create_token(cfg.api_token_file), youtube=youtube, scheduler=scheduler,
            system_stats=SystemStats(), streams=director, topic=topic, vision=vision, learning=learning, researcher=researcher, social=social,
        )
        relay: RelayService | None = None
        if cfg.relay_enabled:  # the online panel: the bot calls OUT to Supabase; nothing listens for the internet
            assert cfg.data_dir is not None
            relay = RelayService(app, cfg, SupabaseClient(cfg.supabase_url, cfg.supabase_publishable_key,
                                                          CredentialStore(cfg.data_dir), transport=relay_transport))
            app.state.container.relay = relay
            if cfg.relay_voice_enabled:
                relay.say_handler = make_voice_handler(app.state.container)

        async def ingest(message):  # noqa: ANN001, ANN202 - what platform adapters call for every incoming message
            await processor.submit(message, wait=False)

        app.state.container.ingest = ingest
        director.ingest = ingest

        await processor.start()
        for adapter in adapters.values():  # each adapter isolates its own failures (a YouTube outage never stops the rest)
            await adapter.start(ingest)
        background = [
            asyncio.create_task(model_manager.warm_up(), name="model-warmup"),
            asyncio.create_task(model_manager.run_health_loop(), name="model-health"),
            asyncio.create_task(retention.run_loop(), name="retention"),
            asyncio.create_task(scheduler.run(), name="proactive-scheduler"),
            asyncio.create_task(session_manager.run_flush_loop(), name="statistics"),
            asyncio.create_task(run_gc_loop(cfg.gc_interval_s), name="memory-housekeeping"),
            asyncio.create_task(processor.run_approval_sweeper(), name="approval-sweeper"),
            asyncio.create_task(director.run(), name="stream-director"),
            asyncio.create_task(vision.run(), name="eyes"),
            asyncio.create_task(learning.run(), name="learning"),
            asyncio.create_task(social.run_loop(), name="social-buzz"),
            asyncio.create_task(topic.run_loop(), name="topic-lookup"),  # looks the topic up at start, after a switch, and keeps headlines fresh
        ]
        if relay is not None:
            background.append(asyncio.create_task(relay.run(), name="online-panel-relay"))
        logger.info("Ready on http://%s:%s (API token file: %s)", cfg.host, cfg.port, cfg.api_token_file)
        try:
            yield
        finally:
            logger.info("Shutting down")
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            if relay is not None:
                await relay.aclose()
            for adapter in adapters.values():
                await adapter.stop()
            await processor.stop()
            if youtube_api:
                await youtube_api.aclose()
            if youtube_http:
                await youtube_http.aclose()
            if kick_http:
                await kick_http.aclose()
            await learning.flush()
            await session_manager.end()
            await ollama.aclose()
            await asyncio.to_thread(engine.dispose)

    return Starlette(
        routes=[*routes.routes, *dashboard.routes],
        lifespan=lifespan,
        # Reject any Host header that is not loopback: defeats DNS-rebinding attacks from web pages.
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])],
        exception_handlers={ValidationFailed: routes.validation_handler},
    )
