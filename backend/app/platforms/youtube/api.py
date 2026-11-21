"""Async client for the YouTube Data API v3: only the methods the bot needs.

Endpoints, parameters, scopes and quota costs were taken from Google's API discovery document (revision 20261117) and the
official reference pages, read on 2026-11-20. Tokens are only ever sent in the Authorization header and never appear in
exception messages or logs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

import httpx

from .quota import QuotaTracker
from .streaming import JsonStreamDecoder, StreamFramingError

logger = logging.getLogger(__name__)
BASE_URL = "https://youtube.googleapis.com/youtube/v3"  # discovery: rootUrl + path
CHAT_PART = "id,snippet,authorDetails"


# ---------------------------------------------------------------------------- errors
class YouTubeApiError(Exception):
    def __init__(self, status: int, reason: str, message: str = "") -> None:
        super().__init__(f"HTTP {status} {reason}: {message}" if message else f"HTTP {status} {reason}")
        self.status, self.reason, self.message = status, reason, message


class AuthError(YouTubeApiError): ...
class QuotaExceeded(YouTubeApiError): ...
class RateLimited(YouTubeApiError): ...
class LiveChatEnded(YouTubeApiError): ...
class LiveChatDisabled(YouTubeApiError): ...
class LiveChatNotFound(YouTubeApiError): ...
class Forbidden(YouTubeApiError): ...
class BadRequest(YouTubeApiError): ...
class ServerError(YouTubeApiError): ...


class NetworkError(YouTubeApiError):
    def __init__(self, message: str) -> None:
        super().__init__(0, "network", message)


_REASON_MAP: dict[str, type[YouTubeApiError]] = {
    "liveChatEnded": LiveChatEnded,
    "liveChatDisabled": LiveChatDisabled,
    "liveChatNotFound": LiveChatNotFound,
    "quotaExceeded": QuotaExceeded,
    "dailyLimitExceeded": QuotaExceeded,
    "rateLimitExceeded": RateLimited,
    "userRateLimitExceeded": RateLimited,
    "authError": AuthError,
}


def classify_error(status: int, body: Any) -> YouTubeApiError:
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    reasons = [e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict)]
    reason = reasons[0] if reasons else str(err.get("status", "")) or f"http_{status}"
    message = str(err.get("message", ""))[:200]
    for r in reasons:
        if r in _REASON_MAP:
            return _REASON_MAP[r](status, r, message)
    if status == 401:
        return AuthError(status, reason, message)
    if status == 429:
        return RateLimited(status, reason, message)
    if status >= 500:
        return ServerError(status, reason, message)
    if status == 403:
        return Forbidden(status, reason, message)
    return BadRequest(status, reason, message)


# ---------------------------------------------------------------------------- data
@dataclass(frozen=True)
class BroadcastInfo:
    video_id: str
    live_chat_id: str
    title: str = ""


@dataclass(frozen=True)
class ChatPage:
    items: list[dict[str, Any]]
    next_page_token: str | None
    polling_interval_s: float
    offline_at: str | None = None


class TokenSource(Protocol):
    async def access_token(self) -> str: ...
    async def force_refresh(self) -> str: ...


# ---------------------------------------------------------------------------- client
class YouTubeApi:
    def __init__(
        self, auth: TokenSource, quota: QuotaTracker, *, transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL, timeout: float = 20.0,
    ) -> None:
        self._auth, self._quota = auth, quota
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, trust_env=False, timeout=httpx.Timeout(timeout, connect=5.0)
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self, method: str, path: str, *, quota_method: str, params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._auth.access_token()
        for attempt in (1, 2):
            try:
                resp = await self._http.request(
                    method, path, params=params, json=json, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TimeoutException as exc:
                raise NetworkError("timeout") from exc
            except httpx.TransportError as exc:
                raise NetworkError(exc.__class__.__name__) from exc
            self._quota.record(quota_method)  # Google bills every request that reaches it, even failed ones
            if resp.status_code == 401 and attempt == 1:
                token = await self._auth.force_refresh()
                continue
            break
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            error = classify_error(resp.status_code, body)
            if isinstance(error, QuotaExceeded):
                self._quota.mark_exhausted()
            raise error
        return body

    # ------------------------------------------------------------------ identity / discovery
    async def own_channel(self) -> dict[str, str]:
        body = await self._call("GET", "/channels", quota_method="channels.list",
                                params={"part": "id,snippet", "mine": "true"})
        items = body.get("items") or []
        if not items:
            raise BadRequest(404, "noChannel", "the authorised Google account has no YouTube channel")
        return {"id": items[0]["id"], "title": items[0].get("snippet", {}).get("title", "")}

    async def active_broadcast(self) -> BroadcastInfo | None:
        """The authorised account's own currently-live broadcast (works only if it owns the stream)."""
        body = await self._call("GET", "/liveBroadcasts", quota_method="liveBroadcasts.list",
                                params={"part": "id,snippet,status", "broadcastStatus": "active", "maxResults": "5"})
        for item in body.get("items", []):
            chat_id = item.get("snippet", {}).get("liveChatId")
            if chat_id:
                return BroadcastInfo(item["id"], chat_id, item.get("snippet", {}).get("title", ""))
        return None

    async def video_chat_id(self, video_id: str) -> str | None:
        body = await self._call("GET", "/videos", quota_method="videos.list",
                                params={"part": "liveStreamingDetails", "id": video_id})
        items = body.get("items") or []
        return items[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if items else None

    async def find_live_video(self, channel_id: str) -> str | None:
        """Currently-live video of a channel (search.list has its own 100-calls/day bucket)."""
        body = await self._call("GET", "/search", quota_method="search.list",
                                params={"part": "snippet", "channelId": channel_id, "eventType": "live",
                                        "type": "video", "maxResults": "1"})
        items = body.get("items") or []
        return items[0].get("id", {}).get("videoId") if items else None

    # ------------------------------------------------------------------ chat
    @staticmethod
    def _page(body: dict[str, Any], default_interval_s: float = 10.0) -> ChatPage:
        interval = body.get("pollingIntervalMillis")
        return ChatPage(
            items=list(body.get("items") or []), next_page_token=body.get("nextPageToken"),
            polling_interval_s=(float(interval) / 1000.0) if interval is not None else default_interval_s,
            offline_at=body.get("offlineAt"),
        )

    async def list_messages(self, live_chat_id: str, page_token: str | None = None) -> ChatPage:
        params = {"liveChatId": live_chat_id, "part": CHAT_PART, "maxResults": "200", "profileImageSize": "16"}
        if page_token:
            params["pageToken"] = page_token
        return self._page(await self._call("GET", "/liveChat/messages", quota_method="liveChatMessages.list",
                                           params=params))

    async def insert_message(self, live_chat_id: str, text: str) -> str:
        body = await self._call(
            "POST", "/liveChat/messages", quota_method="liveChatMessages.insert", params={"part": "snippet"},
            json={"snippet": {"liveChatId": live_chat_id, "type": "textMessageEvent",
                              "textMessageDetails": {"messageText": text}}},
        )
        return str(body.get("id", ""))

    async def stream_messages(self, live_chat_id: str, page_token: str | None = None) -> AsyncIterator[ChatPage]:
        """EXPERIMENTAL HTTP streaming (see streaming.py). Yields one ChatPage per streamed document and ends when the
        server closes the connection. Costs 5 quota units per connection as documented."""
        token = await self._auth.access_token()
        params = {"liveChatId": live_chat_id, "part": CHAT_PART, "profileImageSize": "16"}
        if page_token:
            params["pageToken"] = page_token
        decoder = JsonStreamDecoder()
        try:
            async with self._http.stream(
                "GET", "/liveChat/messages/stream", params=params, headers={"Authorization": f"Bearer {token}"},
                timeout=httpx.Timeout(10.0, read=None),  # a stream is idle between messages; no read timeout
            ) as resp:
                self._quota.record("liveChatMessages.stream")
                if resp.status_code >= 400:
                    raw = await resp.aread()
                    try:
                        body = httpx.Response(resp.status_code, content=raw).json()
                    except ValueError:
                        body = {}
                    error = classify_error(resp.status_code, body)
                    if isinstance(error, QuotaExceeded):
                        self._quota.mark_exhausted()
                    raise error
                async for chunk in resp.aiter_text():
                    for doc in decoder.feed(chunk):
                        if isinstance(doc, dict) and "error" in doc:
                            raise classify_error(int(doc["error"].get("code", 500) or 500), doc)
                        if isinstance(doc, dict):
                            yield self._page(doc)
                decoder.finish()
        except httpx.TimeoutException as exc:
            raise NetworkError("timeout") from exc
        except httpx.TransportError as exc:
            raise NetworkError(exc.__class__.__name__) from exc
        except StreamFramingError as exc:
            raise BadRequest(0, "streamFraming", str(exc)) from exc
