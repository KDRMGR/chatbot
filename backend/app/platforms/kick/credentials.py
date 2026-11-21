"""Stored Kick credentials (DPAPI-encrypted) and access-token refresh."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

import httpx

from ...security.credential_store import CredentialStore
from ..youtube.oauth import ClientConfig, TokenSet
from .oauth import KickOAuthError, refresh_tokens, revoke_token

logger = logging.getLogger(__name__)
CLIENT_KEY = "kick_client"
TOKEN_KEY = "kick_tokens"


class NotAuthorized(Exception):
    """No stored client / tokens: run scripts\\kick_login.py."""


class ReauthRequired(Exception):
    """Kick rejected the refresh token: sign in again with scripts\\kick_login.py."""


class KickAuth:
    def __init__(self, store: CredentialStore, http: httpx.AsyncClient, clock: Callable[[], float] = time.time) -> None:
        self._store, self._http, self._clock = store, http, clock
        self._client: ClientConfig | None = None
        self._tokens: TokenSet | None = None
        self._lock = asyncio.Lock()
        self.reauth_required = False

    def is_configured(self) -> bool:
        return self._store.exists(CLIENT_KEY) and self._store.exists(TOKEN_KEY)

    def load_client(self) -> ClientConfig | None:
        data = self._store.load(CLIENT_KEY)
        return ClientConfig.from_dict(data) if data else None

    def save_login(self, client: ClientConfig, tokens: TokenSet) -> None:
        self._store.save(CLIENT_KEY, client.to_dict())
        self._store.save(TOKEN_KEY, tokens.to_dict())
        self._client, self._tokens, self.reauth_required = client, tokens, False

    def _load(self) -> tuple[ClientConfig, TokenSet]:
        if self._client is None or self._tokens is None:
            client, tokens = self._store.load(CLIENT_KEY), self._store.load(TOKEN_KEY)
            if client is None or tokens is None:
                raise NotAuthorized("Kick is not authorised yet (run scripts\\kick_login.py)")
            self._client, self._tokens = ClientConfig.from_dict(client), TokenSet.from_dict(tokens)
        return self._client, self._tokens

    def scopes(self) -> set[str]:
        """The scopes Kick says were granted (empty if it did not say)."""
        try:
            return set(self._load()[1].scope.split())
        except NotAuthorized:
            return set()

    async def access_token(self) -> str:
        async with self._lock:
            client, tokens = self._load()
            if tokens.expired(self._clock()):
                tokens = await self._refresh(client, tokens)
            return tokens.access_token

    async def force_refresh(self) -> str:
        """After a 401: the access token was rejected although we thought it was valid."""
        async with self._lock:
            client, tokens = self._load()
            return (await self._refresh(client, tokens)).access_token

    async def _refresh(self, client: ClientConfig, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            self.reauth_required = True
            raise ReauthRequired("no refresh token stored")
        try:
            fresh = await refresh_tokens(self._http, client, tokens.refresh_token, self._clock)
        except KickOAuthError as exc:
            if exc.needs_reauth:
                self.reauth_required = True
                logger.error("Kick refresh token was rejected (%s): sign in again", exc.error)
                raise ReauthRequired(exc.error) from exc
            raise
        self._tokens = fresh
        self._store.save(TOKEN_KEY, fresh.to_dict())  # the refresh token may change on every refresh (sliding window)
        self.reauth_required = False
        logger.info("Kick access token refreshed")
        return fresh

    async def revoke_and_forget(self) -> bool:
        """Revoke the grant at Kick, then delete everything stored locally. Returns whether Kick confirmed."""
        revoked = False
        try:
            _, tokens = self._load()
            revoked = await revoke_token(self._http, tokens.refresh_token or tokens.access_token,
                                         "refresh_token" if tokens.refresh_token else "access_token")
        except NotAuthorized:
            pass
        self._store.delete(TOKEN_KEY)
        self._store.delete(CLIENT_KEY)
        self._client = self._tokens = None
        return revoked
