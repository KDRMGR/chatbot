"""A small Supabase client for the bot (Auth password grant + PostgREST), pure httpx.

The bot signs in as its OWN Supabase user (role 'bot' in public.profiles) with email + password stored DPAPI-encrypted outside the
repository (scripts\\relay_login.py). It holds only the PUBLIC project key. It never has the service_role / secret key, so the
database's row-level-security policies (supabase/schema.sql) limit what a stolen bot login could do. Tokens live in memory only and
never appear in logs, errors or reprs.

Endpoints (all documented Supabase APIs):
  POST {url}/auth/v1/token?grant_type=password | refresh_token       sign in / renew
  GET|POST|PATCH|DELETE {url}/rest/v1/<table>?<filters>               PostgREST (eq. filters, order, limit, Prefer headers)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from ..security.credential_store import CredentialStore

logger = logging.getLogger(__name__)
CREDENTIAL_KEY = "supabase_bot"
ROLE_CACHE_S = 60.0


class RelayAuthError(Exception):
    """Signing in failed or the stored login is missing: run scripts\\relay_login.py."""


class RelayHttpError(Exception):
    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}" if status else detail)
        self.status, self.detail = status, detail


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("message") or body.get("msg") or body.get("error_description") or body.get("error") or "")[:160]
    except ValueError:
        pass
    return ""


class SupabaseClient:
    def __init__(self, url: str, public_key: str, store: CredentialStore, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._url, self._key, self._store, self._clock = url.rstrip("/"), public_key, store, clock
        self._http = httpx.AsyncClient(transport=transport, trust_env=False, timeout=15.0)
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        self._roles: dict[str, tuple[float, str | None]] = {}
        self.signed_in_as: str | None = None  # the bot user's e-mail (shown in status; not a secret)

    def __repr__(self) -> str:  # never print tokens
        return f"SupabaseClient(url={self._url!r}, signed_in={self._access is not None})"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth
    def _base_headers(self) -> dict[str, str]:
        return {"apikey": self._key, "Accept": "application/json"}

    async def _token_request(self, grant: str, body: dict[str, str]) -> bool:
        try:
            resp = await self._http.post(f"{self._url}/auth/v1/token", params={"grant_type": grant}, json=body,
                                         headers=self._base_headers())
        except httpx.HTTPError as exc:
            raise RelayHttpError(None, f"could not reach Supabase ({exc.__class__.__name__})") from exc
        if resp.status_code != 200:
            if grant == "refresh_token":
                return False  # the refresh token is gone or expired: sign in again with the password
            raise RelayAuthError(f"Supabase refused the bot's login ({resp.status_code}: {_detail(resp) or 'no reason given'})")
        data = resp.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise RelayAuthError("Supabase's login answer had no access token")
        self._access, self._refresh = data["access_token"], data.get("refresh_token") or self._refresh
        self._expires_at = self._clock() + float(data.get("expires_in") or 3600)
        return True

    async def _ensure_token(self) -> str:
        if self._access is not None and self._clock() < self._expires_at - 60:
            return self._access
        if self._refresh and await self._token_request("refresh_token", {"refresh_token": self._refresh}):
            return self._access  # type: ignore[return-value]
        creds = self._store.load(CREDENTIAL_KEY)
        if not creds or not creds.get("email") or not creds.get("password"):
            raise RelayAuthError("the bot's Supabase login is not stored yet: run scripts\\relay_login.py")
        await self._token_request("password", {"email": creds["email"], "password": creds["password"]})
        self.signed_in_as = creds["email"]
        return self._access  # type: ignore[return-value]

    # ------------------------------------------------------------------ PostgREST
    async def _rest(self, method: str, table: str, *, params: Any = None, json: Any = None, prefer: str | None = None) -> httpx.Response:
        for attempt in (1, 2):
            token = await self._ensure_token()
            headers = {**self._base_headers(), "Authorization": f"Bearer {token}"}
            if prefer:
                headers["Prefer"] = prefer
            try:
                resp = await self._http.request(method, f"{self._url}/rest/v1/{table}", params=params, json=json, headers=headers)
            except httpx.HTTPError as exc:
                raise RelayHttpError(None, f"could not reach Supabase ({exc.__class__.__name__})") from exc
            if resp.status_code == 401 and attempt == 1:  # the token was rejected (clock, revoked): sign in again once
                self._access, self._expires_at = None, 0.0
                continue
            if resp.status_code >= 400:
                raise RelayHttpError(resp.status_code, _detail(resp) or "request failed")
            return resp
        raise RelayHttpError(401, "unauthorised")  # pragma: no cover - the loop always returns or raises

    async def pending_commands(self, limit: int = 20) -> list[dict[str, Any]]:
        resp = await self._rest("GET", "bot_commands", params={"status": "eq.pending", "order": "id.asc", "limit": str(limit),
                                                               "select": "id,created_at,created_by,kind,payload"})
        rows = resp.json()
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def claim(self, command_id: int) -> bool:
        """Mark a pending command as running. The filter on status makes it atomic: only one taker wins."""
        resp = await self._rest("PATCH", "bot_commands", params={"id": f"eq.{int(command_id)}", "status": "eq.pending"},
                                json={"status": "running"}, prefer="return=representation")
        rows = resp.json()
        return isinstance(rows, list) and len(rows) == 1

    async def finish(self, command_id: int, status: str, result: dict[str, Any]) -> None:
        await self._rest("PATCH", "bot_commands", params={"id": f"eq.{int(command_id)}"},
                         json={"status": status, "result": result, "handled_at": datetime.now(timezone.utc).isoformat()})

    async def publish_status(self, data: dict[str, Any]) -> None:
        await self._rest("POST", "bot_status", params={"on_conflict": "id"}, prefer="resolution=merge-duplicates,return=minimal",
                         json={"id": 1, "data": data, "updated_at": datetime.now(timezone.utc).isoformat()})

    async def role_of(self, user_id: str) -> str | None:
        """The role of the user who sent a command (public.profiles); cached for a minute. None = no profile = no rights."""
        now = self._clock()
        hit = self._roles.get(user_id)
        if hit and now - hit[0] < ROLE_CACHE_S:
            return hit[1]
        resp = await self._rest("GET", "profiles", params={"user_id": f"eq.{user_id}", "select": "role"})
        rows = resp.json()
        role = rows[0].get("role") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        self._roles[user_id] = (now, role if isinstance(role, str) else None)
        return self._roles[user_id][1]

    async def prune(self, older_than_iso: str) -> None:
        """Delete finished commands older than the given time (the table is a mailbox, not a log)."""
        await self._rest("DELETE", "bot_commands", params={"handled_at": f"lt.{older_than_iso}"})
