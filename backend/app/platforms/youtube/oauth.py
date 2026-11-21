"""Google OAuth 2.0 for installed (desktop) apps: loopback redirect + PKCE.

Endpoints, parameters and error codes follow https://developers.google.com/identity/protocols/oauth2/native-app
(read 2026-11-20). Nothing here logs or returns secrets in exception messages.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# One scope covers everything the bot needs: liveChatMessages.list/insert, liveBroadcasts.list, videos.list,
# channels.list and search.list (verified against Google's API discovery document, revision 20261117).
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
DEFAULT_SCOPES = (YOUTUBE_SCOPE,)


class OAuthError(Exception):
    """An OAuth failure. ``error`` is Google's error code (e.g. invalid_grant) or one of ours."""

    def __init__(self, error: str, description: str = "", status: int | None = None) -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status = status

    @property
    def needs_reauth(self) -> bool:
        """The refresh token is dead (expired, revoked, ...): the user must sign in again."""
        return self.error == "invalid_grant"


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str | None = None

    @classmethod
    def from_json(cls, text: str) -> "ClientConfig":
        """Parse the 'client_secret_*.json' Google lets you download for an OAuth client."""
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ValueError("not valid JSON") from exc
        body = data.get("installed") or data.get("web") or (data if "client_id" in data else None)
        if not isinstance(body, dict) or not body.get("client_id"):
            raise ValueError("no OAuth client found (expected an 'installed' section with a client_id)")
        return cls(client_id=str(body["client_id"]), client_secret=body.get("client_secret") or None)

    def to_dict(self) -> dict[str, Any]:
        return {"client_id": self.client_id, "client_secret": self.client_secret}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClientConfig":
        return cls(client_id=data["client_id"], client_secret=data.get("client_secret"))


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float  # unix time
    scope: str = ""
    token_type: str = "Bearer"

    def expired(self, now: float, skew: float = 60.0) -> bool:
        return now >= self.expires_at - skew

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token, "refresh_token": self.refresh_token, "expires_at": self.expires_at,
            "scope": self.scope, "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenSet":
        return cls(
            access_token=data["access_token"], refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]), scope=data.get("scope", ""), token_type=data.get("token_type", "Bearer"),
        )

    def __repr__(self) -> str:  # never let a token end up in a log line or traceback
        return f"TokenSet(expires_at={self.expires_at:.0f}, has_refresh_token={self.refresh_token is not None})"


# ---------------------------------------------------------------------------- PKCE / URLs
def generate_pkce() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) using the S256 method (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)  # 86 chars from the unreserved set, within RFC 7636's 43-128
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def build_auth_url(
    client: ClientConfig, redirect_uri: str, scopes: tuple[str, ...], state: str, code_challenge: str,
    login_hint: str | None = None,
) -> str:
    params = {
        "client_id": client.client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(scopes), "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",
        "prompt": "consent",  # make sure a refresh token is issued even if the account authorised this client before
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------- token endpoint
async def _token_request(http: httpx.AsyncClient, data: dict[str, str]) -> dict[str, Any]:
    try:
        resp = await http.post(TOKEN_URL, data=data, headers={"Accept": "application/json"}, timeout=20.0)
    except httpx.HTTPError as exc:
        raise OAuthError("network_error", exc.__class__.__name__) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or "error" in body:
        raise OAuthError(str(body.get("error", f"http_{resp.status_code}")), str(body.get("error_description", "")),
                         resp.status_code)
    return body


def _to_tokens(body: dict[str, Any], now: float, fallback_refresh: str | None = None) -> TokenSet:
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or fallback_refresh,
        expires_at=now + float(body.get("expires_in", 3600)),
        scope=body.get("scope", ""), token_type=body.get("token_type", "Bearer"),
    )


async def exchange_code(
    http: httpx.AsyncClient, client: ClientConfig, code: str, verifier: str, redirect_uri: str,
    clock: Callable[[], float] = time.time,
) -> TokenSet:
    data = {
        "client_id": client.client_id, "code": code, "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    }
    if client.client_secret:
        data["client_secret"] = client.client_secret
    return _to_tokens(await _token_request(http, data), clock())


async def refresh_tokens(
    http: httpx.AsyncClient, client: ClientConfig, refresh_token: str, clock: Callable[[], float] = time.time
) -> TokenSet:
    data = {"client_id": client.client_id, "refresh_token": refresh_token, "grant_type": "refresh_token"}
    if client.client_secret:
        data["client_secret"] = client.client_secret
    return _to_tokens(await _token_request(http, data), clock(), fallback_refresh=refresh_token)


async def revoke_token(http: httpx.AsyncClient, token: str) -> bool:
    """Revoking an access token also revokes its refresh token. Returns True on success."""
    try:
        resp = await http.post(REVOKE_URL, data={"token": token}, timeout=20.0)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


# ---------------------------------------------------------------------------- loopback receiver
_PAGE = (
    "<!doctype html><meta charset=utf-8><title>StealthBot</title>"
    "<body style='font-family:sans-serif;margin:3em'><h2>{title}</h2><p>{body}</p></body>"
)


class LoopbackReceiver:
    """One-shot HTTP listener on 127.0.0.1 (random port) that captures Google's redirect.

    A request with the wrong ``state`` (e.g. from another local process or a malicious page) is rejected with 400
    and ignored; it does not abort the real sign-in. Responses never echo request data.
    """

    def __init__(self, expected_state: str, host: str = "127.0.0.1", *, port: int = 0, callback_path: str = "/",
                 redirect_host: str | None = None) -> None:
        # Google accepts any loopback port and a bare redirect address. Other providers (Kick) need an exact, pre-registered
        # redirect URI, so the port, the path and the host name used in the redirect can be fixed here.
        self._state = expected_state
        self._host = host
        self._port = port
        self._path = callback_path
        self._redirect_host = redirect_host or host
        self._server: asyncio.AbstractServer | None = None
        self._result: asyncio.Future[str] | None = None
        self.redirect_uri = ""

    async def start(self) -> str:
        self._result = asyncio.get_running_loop().create_future()
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        port = self._server.sockets[0].getsockname()[1]
        self.redirect_uri = f"http://{self._redirect_host}:{port}{'' if self._path == '/' else self._path}"
        return self.redirect_uri

    async def wait(self, timeout: float) -> str:
        assert self._result is not None
        try:
            return await asyncio.wait_for(self._result, timeout)
        except asyncio.TimeoutError as exc:
            raise OAuthError("timeout", "no response from the browser; sign-in was not completed") from exc

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _respond(self, writer: asyncio.StreamWriter, status: int, title: str, body: str) -> None:
        payload = _PAGE.format(title=title, body=body).encode("utf-8")
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(payload)}\r\n"
            "Connection: close\r\nCache-Control: no-store\r\n\r\n".encode("ascii") + payload
        )
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert self._result is not None
        try:
            request_line = await asyncio.wait_for(reader.readline(), 5)
            while True:  # discard the headers
                line = await asyncio.wait_for(reader.readline(), 5)
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            target = urllib.parse.urlsplit(parts[1] if len(parts) >= 2 else "/")
            params = dict(urllib.parse.parse_qsl(target.query))
            if target.path != self._path or not ("code" in params or "error" in params):
                await self._respond(writer, 404, "Not found", "Nothing here.")
            elif not hmac.compare_digest(params.get("state", "").encode(), self._state.encode()):
                await self._respond(writer, 400, "Rejected", "This request did not come from the sign-in you started.")
            elif "error" in params:
                if not self._result.done():
                    self._result.set_exception(OAuthError(params["error"], "authorisation was not granted"))
                await self._respond(writer, 200, "Sign-in cancelled", "You can close this window and return to the terminal.")
            else:
                if not self._result.done():
                    self._result.set_result(params["code"])
                await self._respond(writer, 200, "Signed in", "You can close this window and return to the terminal.")
        except (asyncio.TimeoutError, ConnectionError, UnicodeError):
            pass
        finally:
            writer.close()


async def run_loopback_login(
    http: httpx.AsyncClient, client: ClientConfig, scopes: tuple[str, ...] = DEFAULT_SCOPES, *,
    opener: Callable[[str], bool] = webbrowser.open, timeout: float = 300.0, login_hint: str | None = None,
    announce: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
) -> TokenSet:
    """Interactive sign-in: open the browser, wait for Google to redirect back to the local listener, exchange the
    code (with PKCE) for tokens. The USER signs in and approves in their own browser; nothing is typed by this code."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    receiver = LoopbackReceiver(state)
    redirect_uri = await receiver.start()
    try:
        url = build_auth_url(client, redirect_uri, scopes, state, challenge, login_hint)
        announce("Opening your browser to sign in with Google. If it does not open, copy this address into a browser:")
        announce(url)
        if not opener(url):
            announce("(could not open a browser automatically)")
        code = await receiver.wait(timeout)
    finally:
        await receiver.close()
    return await exchange_code(http, client, code, verifier, redirect_uri, clock)
