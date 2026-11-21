"""Kick OAuth 2.1 (authorisation code + PKCE) for an app running on the streamer's own PC.

Endpoints and parameters follow Kick's developer documentation (KickEngineering/KickDevDocs, getting-started/
generating-tokens-oauth2-flow.md and scopes/scopes.md, read 2026-11-21). Points the documentation leaves open are marked
"UNVERIFIED" and must be confirmed by the live sign-in (see docs/phase4-kick.md):
  * the docs say GET for /oauth/authorize, the swagger file says POST: GET is used;
  * the token error body is only documented as {"error": ...}: any 400/401 on a refresh is treated as "sign in again";
  * the client secret is REQUIRED in every token request (nothing says PKCE can replace it).
Secrets never appear in exception messages or reprs.
"""
from __future__ import annotations

import asyncio
import secrets
import time
import urllib.parse
import webbrowser
from typing import Any, Callable

import httpx

from ..youtube.oauth import ClientConfig, LoopbackReceiver, OAuthError, TokenSet, generate_pkce

AUTH_URL = "https://id.kick.com/oauth/authorize"
TOKEN_URL = "https://id.kick.com/oauth/token"
REVOKE_URL = "https://id.kick.com/oauth/revoke"
# user:read   identify the signed-in account      channel:read  the channel and whether it is live
# chat:write  post chat messages (as the bot)      events:subscribe  receive chat messages by webhook
SCOPES = ("user:read", "channel:read", "chat:write", "events:subscribe")
DEFAULT_OAUTH_PORT = 8789  # fixed: Kick compares the redirect URI exactly, so it must be registered once in the app settings
CALLBACK_PATH = "/callback"


class KickOAuthError(OAuthError):
    @property
    def needs_reauth(self) -> bool:
        """A client error on a refresh means the grant is dead (expired, revoked, secret changed): sign in again.
        Server errors and network failures are temporary and are NOT treated that way."""
        return self.status in (400, 401) or self.error in ("invalid_grant", "invalid_request", "invalid_client")


def redirect_uri_for(port: int) -> str:
    return f"http://localhost:{port}{CALLBACK_PATH}"


def build_auth_url(client: ClientConfig, redirect_uri: str, scopes: tuple[str, ...], state: str, code_challenge: str) -> str:
    params = {
        "client_id": client.client_id, "response_type": "code", "redirect_uri": redirect_uri, "state": state,
        "scope": " ".join(scopes), "code_challenge": code_challenge, "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


async def _token_request(http: httpx.AsyncClient, data: dict[str, str]) -> dict[str, Any]:
    try:
        resp = await http.post(TOKEN_URL, data=data, headers={"Accept": "application/json"}, timeout=20.0)
    except httpx.HTTPError as exc:
        raise KickOAuthError("network_error", exc.__class__.__name__) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if resp.status_code != 200 or "error" in body or "access_token" not in body:
        raise KickOAuthError(str(body.get("error", f"http_{resp.status_code}"))[:80], str(body.get("error_description", ""))[:120],
                             resp.status_code)
    return body


def _to_tokens(body: dict[str, Any], now: float, fallback_refresh: str | None = None) -> TokenSet:
    return TokenSet(
        access_token=body["access_token"], refresh_token=body.get("refresh_token") or fallback_refresh,
        expires_at=now + float(body.get("expires_in", 7200)),  # 7200 s is only the example in the swagger file
        scope=body.get("scope", ""), token_type=body.get("token_type", "Bearer"),
    )


def _require_secret(client: ClientConfig) -> str:
    if not client.client_secret:
        raise KickOAuthError("missing_client_secret", "Kick requires the client secret in every token request")
    return client.client_secret


async def exchange_code(
    http: httpx.AsyncClient, client: ClientConfig, code: str, verifier: str, redirect_uri: str,
    clock: Callable[[], float] = time.time,
) -> TokenSet:
    data = {
        "grant_type": "authorization_code", "client_id": client.client_id, "client_secret": _require_secret(client),
        "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri,
    }
    return _to_tokens(await _token_request(http, data), clock())


async def refresh_tokens(
    http: httpx.AsyncClient, client: ClientConfig, refresh_token: str, clock: Callable[[], float] = time.time,
) -> TokenSet:
    data = {"grant_type": "refresh_token", "client_id": client.client_id, "client_secret": _require_secret(client),
            "refresh_token": refresh_token}
    return _to_tokens(await _token_request(http, data), clock(), fallback_refresh=refresh_token)


async def client_credentials(
    http: httpx.AsyncClient, client: ClientConfig, clock: Callable[[], float] = time.time,
) -> TokenSet:
    """An APP access token (documented grant ``client_credentials``): it belongs to the app, not to any account, so it can read
    the public chat events of a channel the app is subscribed to. Verified live 2026-11-22: HTTP 200, Bearer, about 60 days."""
    data = {"grant_type": "client_credentials", "client_id": client.client_id, "client_secret": _require_secret(client)}
    return _to_tokens(await _token_request(http, data), clock())


async def revoke_token(http: httpx.AsyncClient, token: str, hint: str = "refresh_token") -> bool:
    """POST /oauth/revoke (the docs pass token and token_hint_type as query parameters). True if Kick answered 200."""
    try:
        resp = await http.post(REVOKE_URL, params={"token": token, "token_hint_type": hint}, timeout=20.0)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


async def run_loopback_login(
    http: httpx.AsyncClient, client: ClientConfig, *, port: int = DEFAULT_OAUTH_PORT, scopes: tuple[str, ...] = SCOPES,
    opener: Callable[[str], bool] = webbrowser.open, timeout: float = 300.0, announce: Callable[[str], None] = print,
    clock: Callable[[], float] = time.time,
) -> TokenSet:
    """Interactive sign-in: open the browser, wait for Kick to redirect back to http://localhost:<port>/callback, exchange
    the code (with PKCE). The USER signs in and approves in their own browser; nothing is typed by this code."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    receiver = LoopbackReceiver(state, "localhost", port=port, callback_path=CALLBACK_PATH, redirect_host="localhost")
    try:
        redirect_uri = await receiver.start()
    except OSError as exc:
        raise KickOAuthError("port_in_use", f"cannot listen on localhost:{port} ({exc.__class__.__name__})") from exc
    try:
        url = build_auth_url(client, redirect_uri, scopes, state, challenge)
        announce("Opening your browser to sign in to Kick. If it does not open, copy this address into a browser:")
        announce(url)
        if not opener(url):
            announce("(could not open a browser automatically)")
        code = await receiver.wait(timeout)
    finally:
        await receiver.close()
    return await exchange_code(http, client, code, verifier, redirect_uri, clock)
