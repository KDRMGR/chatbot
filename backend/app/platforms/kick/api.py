"""Thin client for Kick's public REST API (https://api.kick.com/public/v1/...).

Every endpoint below is documented in Kick's swagger file (https://api.kick.com/swagger/doc.yaml, read 2026-11-21); where the
documentation is silent the code says so and the live check (scripts\\kick_check.py) decides:
  * GET  /users                    identify the signed-in account (no ``id`` = the token's own user), scope user:read
  * GET  /channels                 the token's own channel (no parameters), scope channel:read; ``stream.is_live``
  * GET  /public-key               PEM public key for webhook signatures; the docs show both {"data": {"public_key": ...}}
                                   and a raw PEM body, so both are accepted
  * GET/POST/DELETE /events/subscriptions   scope events:subscribe; re-subscribing is NOT DOCUMENTED, so callers list first
  * POST /chat                     type "bot" (broadcaster id ignored, posts to the token's channel) or "user" (broadcaster id
                                   required), scope chat:write; content <= 500 characters and <= 2048 bytes
Rate limits are NOT DOCUMENTED (only the 429 status), so 429 is handled with backoff and a Retry-After header if one comes.
Tokens go only in the Authorization header and never into logs, exceptions or reprs.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .credentials import KickAuth, NotAuthorized, ReauthRequired
from .oauth import KickOAuthError

logger = logging.getLogger(__name__)
BASE_URL = "https://api.kick.com"
MAX_CONTENT_BYTES = 2048


class KickApiError(Exception):
    def __init__(self, status: int | None, detail: str = "") -> None:
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status, self.detail = status, detail


class AuthError(KickApiError):
    """401 even after refreshing the token."""


class Forbidden(KickApiError):
    """403: the account or the granted scopes do not allow this."""


class NotFound(KickApiError):
    pass


class BadRequest(KickApiError):
    pass


class RateLimited(KickApiError):
    def __init__(self, status: int | None, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(status, detail)
        self.retry_after = retry_after


class ServerError(KickApiError):
    pass


class NetworkError(KickApiError):
    pass


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("message") or body.get("error") or "")[:200]
    except ValueError:
        pass
    return ""


def classify(resp: httpx.Response) -> KickApiError:
    status, detail = resp.status_code, _detail(resp)
    if status == 401:
        return AuthError(status, detail or "unauthorised")
    if status == 403:
        return Forbidden(status, detail or "forbidden")
    if status == 404:
        return NotFound(status, detail or "not found")
    if status == 429:
        retry = resp.headers.get("Retry-After", "")
        return RateLimited(status, detail or "too many requests", float(retry) if retry.replace(".", "", 1).isdigit() else None)
    if 400 <= status < 500:
        return BadRequest(status, detail)
    return ServerError(status, detail)


def parse_public_key_body(text: str, content_type: str = "") -> str:
    """The PEM key, whether Kick answered with JSON {"data": {"public_key": ...}} or with the raw PEM text."""
    stripped = text.strip()
    if stripped.startswith("-----BEGIN"):
        return stripped
    try:
        import json

        data = json.loads(stripped)
    except ValueError as exc:
        raise BadRequest(200, "the public key answer was neither JSON nor PEM") from exc
    key = (data.get("data") or {}).get("public_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or "-----BEGIN" not in key:
        raise BadRequest(200, "the public key answer had no key in it")
    return key.strip()


class KickApi:
    def __init__(self, auth: KickAuth, http: httpx.AsyncClient | None = None, *, base_url: str = BASE_URL,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._auth = auth
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, transport=transport, trust_env=False, timeout=20.0)
        self._base = base_url

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ plumbing
    async def _send(self, method: str, path: str, *, token: str | None, params: Any = None, json: Any = None) -> httpx.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            return await self._http.request(method, path if path.startswith("http") else self._base + path, params=params,
                                            json=json, headers=headers)
        except httpx.TimeoutException as exc:
            raise NetworkError(None, "timeout") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(None, exc.__class__.__name__) from exc

    async def _request(self, method: str, path: str, *, params: Any = None, json: Any = None, auth: bool = True) -> httpx.Response:
        token = await self._auth.access_token() if auth else None
        resp = await self._send(method, path, token=token, params=params, json=json)
        if resp.status_code == 401 and auth:  # the token we thought valid was rejected: refresh once and retry
            try:
                token = await self._auth.force_refresh()
            except KickOAuthError as exc:
                raise AuthError(401, f"refresh failed ({exc.error})") from exc
            resp = await self._send(method, path, token=token, params=params, json=json)
        if resp.status_code >= 400:
            raise classify(resp)
        return resp

    @staticmethod
    def _data(resp: httpx.Response) -> Any:
        try:
            body = resp.json()
        except ValueError as exc:
            raise BadRequest(resp.status_code, "the answer was not JSON") from exc
        return body.get("data") if isinstance(body, dict) else None

    # ------------------------------------------------------------------ identity
    async def me(self) -> dict[str, Any]:
        """The signed-in account: {"user_id": int, "name": str} (GET /users without ``id``)."""
        data = self._data(await self._request("GET", "/public/v1/users"))
        if not isinstance(data, list) or not data or not isinstance(data[0], dict) or data[0].get("user_id") is None:
            raise BadRequest(200, "no user in the answer")
        return {"user_id": int(data[0]["user_id"]), "name": str(data[0].get("name") or "")}

    async def channel(self) -> dict[str, Any]:
        """The signed-in account's channel (GET /channels without parameters)."""
        data = self._data(await self._request("GET", "/public/v1/channels"))
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise BadRequest(200, "no channel in the answer")
        ch = data[0]
        stream = ch.get("stream") if isinstance(ch.get("stream"), dict) else {}
        return {"broadcaster_user_id": ch.get("broadcaster_user_id"), "slug": str(ch.get("slug") or ""),
                "is_live": bool(stream.get("is_live"))}

    async def public_key(self) -> str:
        resp = await self._request("GET", "/public/v1/public-key", auth=False)
        return parse_public_key_body(resp.text, resp.headers.get("content-type", ""))

    # ------------------------------------------------------------------ events
    async def list_subscriptions(self) -> list[dict[str, Any]]:
        data = self._data(await self._request("GET", "/public/v1/events/subscriptions"))
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    async def subscribe(self, events: list[tuple[str, int]]) -> list[dict[str, Any]]:
        """Subscribe by webhook. Returns one result per event ({name, version, subscription_id, error})."""
        body = {"events": [{"name": n, "version": v} for n, v in events], "method": "webhook"}
        data = self._data(await self._request("POST", "/public/v1/events/subscriptions", json=body))
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    async def unsubscribe(self, ids: list[str]) -> None:
        if ids:
            await self._request("DELETE", "/public/v1/events/subscriptions", params=[("id", i) for i in ids])

    # ------------------------------------------------------------------ chat
    async def send_chat(self, content: str, *, kind: str = "bot", broadcaster_user_id: int | None = None,
                        reply_to: str | None = None) -> dict[str, Any]:
        """Post one message. Returns {"is_sent": bool, "message_id": str|None}."""
        if kind not in ("bot", "user"):
            raise ValueError("kind must be 'bot' or 'user'")
        if kind == "user" and broadcaster_user_id is None:
            raise ValueError("a message sent as 'user' needs the broadcaster's user id")
        text = content
        while len(text.encode("utf-8")) > MAX_CONTENT_BYTES:  # documented limit: 2048 bytes as well as 500 characters
            text = text[:-1]
        body: dict[str, Any] = {"content": text[:500], "type": kind}
        if kind == "user":
            body["broadcaster_user_id"] = int(broadcaster_user_id)  # type: ignore[arg-type]
        if reply_to:
            body["reply_to_message_id"] = reply_to
        data = self._data(await self._request("POST", "/public/v1/chat", json=body))
        if not isinstance(data, dict):
            raise BadRequest(200, "the chat answer had no data")
        return {"is_sent": bool(data.get("is_sent")), "message_id": data.get("message_id")}
