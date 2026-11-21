"""Test doubles for the YouTube integration: a fake Google (OAuth + Data API over httpx.MockTransport) and a scripted
fake of the API client used to drive the adapter's state machine deterministically."""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections import defaultdict
from typing import Any, AsyncIterator, Callable

import httpx

from app.platforms.youtube.api import BroadcastInfo, ChatPage


def error_body(status: int, reason: str, message: str = "") -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": status, "message": message or reason,
                              "errors": [{"reason": reason, "domain": "youtube.test"}]}}


def chat_item(
    mid: str, text: str = "hello there", channel: str = "UC_VIEWER", name: str = "Viewer", kind: str = "textMessageEvent",
    **extra: Any,
) -> dict[str, Any]:
    snippet: dict[str, Any] = {"type": kind, "liveChatId": "CHAT1", "authorChannelId": channel,
                               "publishedAt": "2026-11-20T10:00:00.123456+00:00", "displayMessage": text}
    if kind == "textMessageEvent":
        snippet["textMessageDetails"] = {"messageText": text}
    snippet.update(extra)
    return {"kind": "youtube#liveChatMessage", "id": mid, "snippet": snippet,
            "authorDetails": {"channelId": channel, "displayName": name, "isChatOwner": False,
                              "isChatModerator": False}}


def chat_page(items: list[dict[str, Any]], token: str = "tok", interval_ms: int = 100, **extra: Any) -> dict[str, Any]:
    return {"kind": "youtube#liveChatMessageListResponse", "items": items, "nextPageToken": token,
            "pollingIntervalMillis": interval_ms, **extra}


class FakeGoogle:
    """Handles oauth2.googleapis.com and youtube.googleapis.com requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_requests: list[dict[str, str]] = []
        self.revoked: list[str] = []
        self.valid_tokens: set[str] = {"access-1"}
        self.reject_all = False  # reject even freshly refreshed tokens (a revoked or mis-scoped grant)
        self.refresh_count = 0
        self.refresh_error: tuple[int, dict[str, Any]] | None = None
        self.channel = {"id": "UC_BOT", "title": "StealthBot"}
        self.broadcasts: list[dict[str, Any]] = [{"id": "VID1", "snippet": {"liveChatId": "CHAT1", "title": "Live!"}}]
        self.videos: dict[str, str | None] = {"VID1": "CHAT1"}
        self.search_video: str | None = None
        self.chat_pages: list[Any] = []  # dict (page) or (status, body) tuple (error), consumed in order
        self.list_params: list[dict[str, str]] = []
        self.inserted: list[dict[str, Any]] = []
        self.insert_error: tuple[int, dict[str, Any]] | None = None
        self.errors: dict[str, tuple[int, dict[str, Any]]] = {}  # "GET /youtube/v3/channels" -> forced error (sticky)
        self.stream_body: bytes = b"[]"
        self.stream_status = 200
        self._n = 0

    def calls(self, method: str, path_suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(path_suffix)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "oauth2.googleapis.com":
            return self._oauth(request, path)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if self.reject_all or token not in self.valid_tokens:
            return httpx.Response(401, json=error_body(401, "authError", "Invalid Credentials")[1])
        forced = self.errors.get(f"{request.method} {path}")
        if forced:
            return httpx.Response(forced[0], json=forced[1])
        if path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": self.channel["id"],
                                                        "snippet": {"title": self.channel["title"]}}]})
        if path.endswith("/liveBroadcasts"):
            return httpx.Response(200, json={"items": self.broadcasts})
        if path.endswith("/videos"):
            chat = self.videos.get(request.url.params.get("id", ""))
            return httpx.Response(200, json={"items": [{"liveStreamingDetails": {"activeLiveChatId": chat}}]
                                             if request.url.params.get("id") in self.videos else []})
        if path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": self.search_video}}] if self.search_video else []})
        if path.endswith("/liveChat/messages/stream"):
            return httpx.Response(self.stream_status, content=self.stream_body,
                                  headers={"content-type": "application/json"})
        if path.endswith("/liveChat/messages") and request.method == "GET":
            self.list_params.append(dict(request.url.params))
            self._n += 1
            page = self.chat_pages.pop(0) if self.chat_pages else chat_page([], f"tok-{self._n}")
            if isinstance(page, tuple):
                return httpx.Response(page[0], json=page[1])
            return httpx.Response(200, json=page)
        if path.endswith("/liveChat/messages") and request.method == "POST":
            if self.insert_error:
                return httpx.Response(self.insert_error[0], json=self.insert_error[1])
            body = json.loads(request.content)
            self.inserted.append({"params": dict(request.url.params), "body": body})
            return httpx.Response(200, json={"id": f"MSG-{len(self.inserted)}", "snippet": body["snippet"]})
        return httpx.Response(404, json=error_body(404, "notFound")[1])

    def _oauth(self, request: httpx.Request, path: str) -> httpx.Response:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
        if path == "/revoke":
            self.revoked.append(form.get("token", ""))
            return httpx.Response(200, json={})
        self.token_requests.append(form)
        if form.get("grant_type") == "refresh_token":
            if self.refresh_error:
                return httpx.Response(self.refresh_error[0], json=self.refresh_error[1])
            self.refresh_count += 1
            token = f"access-r{self.refresh_count}"
            self.valid_tokens.add(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600, "token_type": "Bearer",
                                             "scope": "https://www.googleapis.com/auth/youtube.force-ssl"})
        self.valid_tokens.add("access-new")
        return httpx.Response(200, json={"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 3599,
                                         "token_type": "Bearer", "scope": "https://www.googleapis.com/auth/youtube.force-ssl"})


class Sleeper:
    """Records requested delays and yields to the event loop instead of really waiting."""

    def __init__(self, real: float = 0.001) -> None:
        self.delays: list[float] = []
        self._real = real

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(self._real)


async def eventually(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


class FakeApi:
    """Scripted stand-in for YouTubeApi. Each queue entry is a value to return or an Exception to raise."""

    def __init__(self) -> None:
        self.script: dict[str, list[Any]] = defaultdict(list)
        self.calls: list[tuple[str, Any]] = []
        self.stream_pages: list[list[Any]] = []  # one list of pages per stream connection

    def _next(self, name: str, default: Any) -> Any:
        queue = self.script[name]
        item = queue.pop(0) if queue else default
        if isinstance(item, BaseException):
            raise item
        return item

    async def own_channel(self) -> dict[str, str]:
        self.calls.append(("own_channel", None))
        return self._next("own_channel", {"id": "UC_BOT", "title": "StealthBot"})

    async def active_broadcast(self) -> BroadcastInfo | None:
        self.calls.append(("active_broadcast", None))
        return self._next("active_broadcast", BroadcastInfo("VID1", "CHAT1", "Live!"))

    async def video_chat_id(self, video_id: str) -> str | None:
        self.calls.append(("video_chat_id", video_id))
        return self._next("video_chat_id", "CHAT-FROM-VIDEO")

    async def find_live_video(self, channel_id: str) -> str | None:
        self.calls.append(("find_live_video", channel_id))
        return self._next("find_live_video", "VID-FOUND")

    async def list_messages(self, chat_id: str, page_token: str | None = None) -> ChatPage:
        self.calls.append(("list_messages", (chat_id, page_token)))
        return self._next("list_messages", ChatPage([], "tok-default", 0.05))

    async def insert_message(self, chat_id: str, text: str) -> str:
        self.calls.append(("insert_message", (chat_id, text)))
        return self._next("insert_message", "MSG-1")

    async def stream_messages(self, chat_id: str, page_token: str | None = None) -> AsyncIterator[ChatPage]:
        self.calls.append(("stream_messages", (chat_id, page_token)))
        pages = self.stream_pages.pop(0) if self.stream_pages else []
        for page in pages:
            if isinstance(page, BaseException):
                raise page
            yield page

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)
