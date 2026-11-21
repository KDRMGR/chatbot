from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import httpx
import pytest
import pytest_asyncio

from app.config import RateLimitConfig, Settings
from app.main import create_app

REPLIES = [
    "Welcome in! Great to see you here today, what are you playing lately?",
    "That supercar plan sounds wild, I say go for it and see what happens!",
    "Ha, that crash was spectacular, the lamp post never saw it coming!",
    "Honestly the fastest car depends on the day, what is your personal favourite?",
    "Chat, quick vote: helicopter escape or boat escape from the cops?",
    "Okay that was smooth, the streamer is cooking right now, keep it going!",
    "Thanks for hanging out with us, tell me your best moment of the stream so far.",
    "Bold strategy, let us see whether it pays off or ends in a spectacular fireball.",
]


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeOllama:
    """Stands in for Ollama's HTTP API via httpx.MockTransport. Configure failures per model."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.installed = ["primary-model", "fallback-model"]
        self.unloaded: list[str] = []
        self.fail_models: dict[str, str] = {}  # model -> "500" | "timeout" | "gpu500" | "404"
        self.version_ok = True
        self.reply: str | Callable[[dict[str, Any]], str] | None = None
        self._n = 0

    @property
    def real_calls(self) -> list[dict[str, Any]]:
        """Chat calls excluding the one-token warm-up the app performs at startup."""
        return [c for c in self.chat_calls if c["options"].get("num_predict") != 1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if not self.version_ok:
            raise httpx.ConnectError("connection refused")
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.0-test"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n} for n in self.installed]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/generate":  # the app uses this only to unload a model (keep_alive 0)
            self.unloaded.append(json.loads(request.content)["model"])
            return httpx.Response(200, json={})
        if path == "/api/chat":
            body = json.loads(request.content)
            self.chat_calls.append(body)
            mode = self.fail_models.get(body["model"])
            if mode == "500" or (mode == "gpu500" and "num_gpu" in body["options"]):
                return httpx.Response(500, text="boom")
            if mode == "404":
                return httpx.Response(404, text="model not found")
            if mode == "timeout":
                raise httpx.ReadTimeout("too slow")
            if body["options"].get("num_predict") == 1:
                text = "hi"
            elif callable(self.reply):
                text = self.reply(body)
            elif self.reply is not None:
                text = self.reply
            else:
                text = REPLIES[self._n % len(REPLIES)]
                self._n += 1
            return httpx.Response(200, json={
                "message": {"role": "assistant", "content": text},
                "eval_count": 20, "eval_duration": 400_000_000, "done_reason": "stop",
            })
        return httpx.Response(404, text="unknown path")


@pytest.fixture(autouse=True)
def _close_log_handlers():
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_stealthbot", False):
            root.removeHandler(h)
            h.close()


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def make_settings(tmp_path):
    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = dict(
            state_dir=tmp_path, primary_model="primary-model", fallback_model="fallback-model",
            generation_timeout_s=5, generation_deadline_s=10, warmup_timeout_s=10,
            model_recheck_interval_s=60, health_interval_s=3600,
        )
        base.update(overrides)
        return Settings(**base)
    return _make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest_asyncio.fixture
async def make_app_client(make_settings, fake_ollama):
    """Factory: (settings overrides) -> (client, app). Runs the real lifespan (migrations, worker, ...)."""
    stack: list[Any] = []

    async def _make(*, keep_cooldowns: bool = False, youtube: Any = None, youtube_credentials: bool = True,
                    kick: Any = None, **overrides: Any):
        if kick is not None:
            overrides.setdefault("kick_enabled", True)
            overrides.setdefault("kick_webhook_port", 0)  # any free port
            overrides.setdefault("kick_resubscribe_interval_s", 0.1)
        settings = make_settings(**overrides)
        if kick is not None:  # pre-authorise like scripts\kick_login.py would (real DPAPI encryption into the test's state dir)
            from app.platforms.kick.credentials import CLIENT_KEY as KICK_CLIENT, TOKEN_KEY as KICK_TOKEN
            from app.platforms.youtube.oauth import TokenSet as KickTokenSet
            from app.security.credential_store import CredentialStore as KickStore

            kick_store = KickStore(settings.data_dir)
            kick_store.save(KICK_CLIENT, {"client_id": "cid", "client_secret": "csecret"})
            kick_store.save(KICK_TOKEN, KickTokenSet("access-0", "refresh-0", time.time() + 3600,
                                                     scope="user:read channel:read chat:write events:subscribe").to_dict())
        if youtube is not None and youtube_credentials:
            # pre-authorise like scripts\youtube_login.py would (real DPAPI encryption into the test's state dir)
            from app.platforms.youtube.credentials import CLIENT_KEY, TOKEN_KEY
            from app.platforms.youtube.oauth import TokenSet
            from app.security.credential_store import CredentialStore

            store = CredentialStore(settings.data_dir)
            store.save(CLIENT_KEY, {"client_id": "cid", "client_secret": "csecret"})
            store.save(TOKEN_KEY, TokenSet("access-1", "refresh-1", time.time() + 3600).to_dict())
        app = create_app(
            settings, ollama_transport=httpx.MockTransport(fake_ollama.handler),
            youtube_transport=httpx.MockTransport(youtube.handler) if youtube is not None else None,
            kick_transport=httpx.MockTransport(kick.handler) if kick is not None else None,
        )
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        if not keep_cooldowns:
            # Personality cooldowns are real (20 s for ChatBuddy); zero them so back-to-back test messages
            # aren't dropped. Cooldown behaviour itself has dedicated tests.
            for profile in app.state.container.personalities.all():
                profile.cooldown_seconds = 0
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
        client.headers["X-API-Key"] = app.state.container.api_token
        stack.append((ctx, client))
        return client, app

    yield _make
    for ctx, client in reversed(stack):
        await client.aclose()
        await ctx.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def app_client(make_app_client):
    return await make_app_client()


# convenient re-export for tests that tweak rate limits
__all__ = ["FakeClock", "FakeOllama", "RateLimitConfig"]
