"""The webhook receiver: the ONE part of STEALTHBOT that the internet can reach (through a tunnel such as ngrok or Cloudflare).

Design rules, because of that:
  * it is a separate server on its own loopback port with exactly one route that does anything (POST /kick/webhook); the
    control API and the dashboard are NOT reachable through it, ever;
  * nothing is trusted until the signature has been verified (RSA PKCS#1 v1.5 / SHA-256 over ``message_id.timestamp.body`` with
    Kick's public key, fetched from the API and re-fetched at most once a minute if verification fails, since Kick may rotate it);
  * the timestamp must be recent and a message id is accepted once (Kick retries deliveries and uses the id as an idempotency key);
  * bodies are size-limited, answers never echo request data, and a failure to reach Kick's key endpoint is a 503 so Kick retries.
Header names, the signed string and the algorithm are from Kick's docs (events/webhook-security.md, read 2026-11-21). Kick says it
unsubscribes an app whose webhook keeps failing for more than a day, so errors are answered precisely and processing problems
on our side are NOT turned into error responses (the event was valid; it is acknowledged).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import socket
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .rsa_verify import KeyError_, parse_public_key, verify_pkcs1_v15_sha256

logger = logging.getLogger(__name__)

H_SIGNATURE = "kick-event-signature"
H_MESSAGE_ID = "kick-event-message-id"
H_TIMESTAMP = "kick-event-message-timestamp"
H_TYPE = "kick-event-type"
H_VERSION = "kick-event-version"
WEBHOOK_PATH = "/kick/webhook"
MAX_BODY_BYTES = 64 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")  # a ULID is 26 characters; be tolerant but never accept odd bytes


class WebhookRejected(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class VerifiedEvent:
    message_id: str  # Kick-Event-Message-Id (idempotency key)
    event_type: str
    version: int
    payload: dict[str, Any]


def parse_timestamp(value: str) -> float:
    """RFC 3339 -> unix seconds. Raises ValueError."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class WebhookVerifier:
    def __init__(
        self, fetch_key: Callable[[], Awaitable[str]], *, tolerance_s: float = 600.0, clock: Callable[[], float] = time.time,
        refetch_min_interval_s: float = 60.0, seen_max: int = 5000,
    ) -> None:
        self._fetch_key = fetch_key
        self._tolerance, self._clock, self._refetch_gap = tolerance_s, clock, refetch_min_interval_s
        self._key: tuple[int, int] | None = None
        self._fetched_at: float | None = None
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._seen_max = seen_max
        self.counters = {"accepted": 0, "duplicates": 0, "rejected": 0, "key_fetches": 0}

    async def _load_key(self, *, force: bool = False) -> tuple[int, int]:
        now = self._clock()
        if self._key is not None and not force:
            return self._key
        if force and self._fetched_at is not None and now - self._fetched_at < self._refetch_gap:
            return self._key  # type: ignore[return-value] # asked too soon: do not let bad requests make us hammer Kick's API
        try:
            pem = await self._fetch_key()
            key = parse_public_key(pem)
        except KeyError_ as exc:
            raise WebhookRejected(503, "the public key from Kick could not be understood") from exc
        except Exception as exc:  # noqa: BLE001 - any failure to get the key: let Kick retry later
            raise WebhookRejected(503, "could not fetch Kick's public key") from exc
        self._key, self._fetched_at = key, now
        self.counters["key_fetches"] += 1
        return key

    def _remember(self, message_id: str) -> bool:
        """True if the id is new (and is now remembered); False if it was seen before."""
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > self._seen_max:
            self._seen.discard(self._seen_order.popleft())
        return True

    async def verify(self, headers: Mapping[str, str], body: bytes) -> tuple[VerifiedEvent, bool]:
        """Check one delivery. Returns (event, is_new). Raises WebhookRejected with the HTTP status to answer."""
        try:
            return await self._verify(headers, body)
        except WebhookRejected:
            self.counters["rejected"] += 1
            raise

    async def _verify(self, headers: Mapping[str, str], body: bytes) -> tuple[VerifiedEvent, bool]:
        h = {k.lower(): v for k, v in headers.items()}
        message_id, timestamp, signature_b64 = h.get(H_MESSAGE_ID, ""), h.get(H_TIMESTAMP, ""), h.get(H_SIGNATURE, "")
        if not (message_id and timestamp and signature_b64):
            raise WebhookRejected(400, "missing signature headers")
        if not _ID_RE.match(message_id):
            raise WebhookRejected(400, "bad message id")
        try:
            signature = base64.b64decode(signature_b64, validate=True)
            sent_at = parse_timestamp(timestamp)
        except (binascii.Error, ValueError) as exc:
            raise WebhookRejected(400, "bad signature or timestamp format") from exc
        if abs(self._clock() - sent_at) > self._tolerance:
            raise WebhookRejected(401, "timestamp outside the allowed window")
        signed = message_id.encode("ascii") + b"." + timestamp.encode("ascii") + b"." + body  # messageID.timestamp.body

        key = await self._load_key()
        ok = verify_pkcs1_v15_sha256(key, signed, signature)
        if not ok:  # maybe Kick rotated its key: fetch once (rate limited) and try again
            fresh = await self._load_key(force=True)
            ok = fresh != key and verify_pkcs1_v15_sha256(fresh, signed, signature)
        if not ok:
            raise WebhookRejected(401, "signature does not match")

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise WebhookRejected(400, "body is not JSON") from exc
        if not isinstance(payload, dict):
            raise WebhookRejected(400, "body is not a JSON object")
        event_type = h.get(H_TYPE, "")
        try:
            version = int(h.get(H_VERSION, "1"))
        except ValueError:
            version = 1
        is_new = self._remember(message_id)
        self.counters["accepted" if is_new else "duplicates"] += 1
        return VerifiedEvent(message_id, event_type, version, payload), is_new


# ---------------------------------------------------------------------------- the HTTP app
def build_webhook_app(
    verifier: WebhookVerifier, on_event: Callable[[VerifiedEvent], Awaitable[None]], *, max_body: int = MAX_BODY_BYTES,
) -> Starlette:
    async def receive(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > max_body):
            return JSONResponse({"detail": "body too large"}, status_code=413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_body:
                return JSONResponse({"detail": "body too large"}, status_code=413)
            chunks.append(chunk)
        try:
            event, is_new = await verifier.verify(request.headers, b"".join(chunks))
        except WebhookRejected as exc:
            logger.warning("Kick webhook rejected (%d): %s", exc.status, exc.reason)
            return JSONResponse({"detail": exc.reason}, status_code=exc.status)
        if is_new:
            try:
                await on_event(event)
            except Exception:  # noqa: BLE001 - the delivery was valid; our own trouble must not make Kick unsubscribe us
                logger.exception("could not process a Kick event")
        return JSONResponse({"ok": True})

    async def health(request: Request) -> PlainTextResponse:
        """Lets you check that the tunnel reaches this program: open <your public URL>/kick/health in a browser."""
        return PlainTextResponse("stealthbot kick webhook: ok\n")

    return Starlette(routes=[Route(WEBHOOK_PATH, receive, methods=["POST"]), Route("/kick/health", health, methods=["GET"])])


class _EmbeddedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):  # noqa: ANN201 - the main server owns Ctrl+C and shutdown; this one must not touch signal handlers
        yield


class WebhookServer:
    """Runs the webhook app inside the bot's own event loop, on 127.0.0.1 only (the tunnel connects from this PC)."""

    def __init__(self, app: Starlette, port: int) -> None:
        self._app, self.port = app, port
        self._server: _EmbeddedServer | None = None
        self._task: Any = None
        self._sock: socket.socket | None = None

    async def _serve(self) -> None:
        assert self._server is not None
        try:
            await self._server.serve(sockets=[self._sock] if self._sock else None)
        except SystemExit:  # uvicorn calls sys.exit() on a failed start; inside a task that would take the whole bot down
            logger.error("the Kick webhook server stopped during start-up")

    async def start(self) -> None:
        # Bind here, not inside uvicorn: a port that is already taken is then an ordinary OSError we can report and retry.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.port))  # loopback only: the tunnel program connects from this PC
        except OSError:
            sock.close()
            raise
        sock.setblocking(False)
        self.port = sock.getsockname()[1]  # the real port (a configured 0 means "any free port", used by the tests)
        self._sock = sock
        config = uvicorn.Config(self._app, log_level="warning", access_log=False, lifespan="off", server_header=False,
                                date_header=False)
        self._server = _EmbeddedServer(config)
        self._task = asyncio.create_task(self._serve(), name="kick-webhook-server")
        for _ in range(100):  # up to 5 s
            if self._server.started:
                return
            if self._task.done():
                break
            await asyncio.sleep(0.05)
        await self.stop()
        raise OSError(f"the Kick webhook listener could not start on 127.0.0.1:{self.port}")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self._task.cancel()
            self._task = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._server = None

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.started and self._task is not None and not self._task.done()
