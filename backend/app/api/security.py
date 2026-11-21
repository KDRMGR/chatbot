"""Local API authentication. The server only binds to loopback, but any web page the streamer visits can still
send requests to http://127.0.0.1, so every state-changing or data-returning route requires a secret token
(plus the Host header is validated in main.py to defeat DNS-rebinding)."""
from __future__ import annotations

import functools
import hmac
import secrets
import time
from pathlib import Path
from typing import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


def load_or_create_token(path: Path) -> str:
    """Token is stored in the user's private %LOCALAPPDATA% folder, never in the repo, logs or .env."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


def has_valid_api_key(request: Request) -> bool:
    supplied = request.headers.get("x-api-key", "")
    expected: str = request.app.state.container.api_token
    return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class TicketStore:
    """One-time sign-in tickets for opening the dashboard.

    A launcher script that can read the token file asks for a ticket and opens the dashboard with it; the page swaps
    the ticket for the API key. The ticket lives 60 seconds and works once, so the browser history never keeps
    anything usable (the real key is never put into a URL).
    """

    def __init__(self, *, ttl_s: float = 60.0, max_outstanding: int = 5, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._max, self._clock = ttl_s, max_outstanding, clock
        self._tickets: dict[str, float] = {}

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, exp in self._tickets.items() if exp <= now]:
            del self._tickets[key]

    def issue(self) -> str:
        self._expire()
        while len(self._tickets) >= self._max:  # drop the oldest instead of growing without bound
            del self._tickets[next(iter(self._tickets))]
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = self._clock() + self._ttl
        return ticket

    def redeem(self, ticket: str) -> bool:
        self._expire()
        for known in list(self._tickets):  # constant-time comparison against every outstanding ticket
            if hmac.compare_digest(known.encode("utf-8"), ticket.encode("utf-8")):
                del self._tickets[known]  # single use
                return True
        return False


Handler = Callable[[Request], Awaitable[Response]]


def protected(handler: Handler) -> Handler:
    """Decorator: reject with 401 before the handler (or any body parsing) runs."""

    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        if not has_valid_api_key(request):
            return JSONResponse({"detail": "Missing or invalid X-API-Key"}, status_code=401)
        return await handler(request)

    return wrapper
