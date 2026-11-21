"""Test helpers for the Kick connector: a throwaway RSA key pair (the app itself can only VERIFY, so signing lives here),
a fake Kick (OAuth + REST API) behind httpx.MockTransport, and a webhook signer."""
from __future__ import annotations

import base64
import hashlib
import json
import random
from typing import Any

import httpx

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


# ---------------------------------------------------------------------------- tiny RSA for tests
def _is_probable_prime(n: int, rng: random.Random, rounds: int = 24) -> bool:
    if n < 4:
        return n in (2, 3)
    if n % 2 == 0:
        return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = rng.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate, rng):
            return candidate


class TestKey:
    __test__ = False  # not a pytest test class

    def __init__(self, bits: int = 1024, seed: int = 1) -> None:
        rng = random.Random(seed)
        e = 65537
        while True:
            p, q = _prime(bits // 2, rng), _prime(bits // 2, rng)
            phi = (p - 1) * (q - 1)
            if p != q and phi % e != 0:
                break
        self.n, self.e, self.d = p * q, e, pow(e, -1, phi)
        self.k = (self.n.bit_length() + 7) // 8

    def sign(self, message: bytes) -> bytes:
        tail = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
        block = b"\x00\x01" + b"\xff" * (self.k - len(tail) - 3) + b"\x00" + tail
        return pow(int.from_bytes(block, "big"), self.d, self.n).to_bytes(self.k, "big")

    # ---- DER / PEM encoding of the public key
    @staticmethod
    def _der(tag: int, content: bytes) -> bytes:
        n = len(content)
        length = bytes([n]) if n < 0x80 else (b"\x81" + bytes([n]) if n < 0x100 else b"\x82" + n.to_bytes(2, "big"))
        return bytes([tag]) + length + content

    @classmethod
    def _int(cls, value: int) -> bytes:
        raw = value.to_bytes((value.bit_length() + 8) // 8, "big")
        return cls._der(0x02, raw)

    def pkcs1_der(self) -> bytes:
        return self._der(0x30, self._int(self.n) + self._int(self.e))

    def spki_pem(self) -> str:
        algorithm = self._der(0x30, self._der(0x06, bytes.fromhex("2a864886f70d010101")) + self._der(0x05, b""))
        spki = self._der(0x30, algorithm + self._der(0x03, b"\x00" + self.pkcs1_der()))
        body = base64.b64encode(spki).decode()
        return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END PUBLIC KEY-----\n"

    def pkcs1_pem(self) -> str:
        body = base64.b64encode(self.pkcs1_der()).decode()
        return "-----BEGIN RSA PUBLIC KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END RSA PUBLIC KEY-----\n"


def signed_headers(key: TestKey, message_id: str, timestamp: str, body: bytes, event_type: str = "chat.message.sent") -> dict[str, str]:
    signature = key.sign(message_id.encode() + b"." + timestamp.encode() + b"." + body)
    return {
        "Kick-Event-Message-Id": message_id, "Kick-Event-Message-Timestamp": timestamp, "Kick-Event-Type": event_type,
        "Kick-Event-Version": "1", "Kick-Event-Signature": base64.b64encode(signature).decode(), "Content-Type": "application/json",
    }


def chat_payload(text: str = "hello everyone!", *, user_id: int = 4242, username: str = "Alice", message_id: str = "11111111-2222-3333-4444-555555555555",
                 broadcaster_id: int = 777) -> dict[str, Any]:
    return {
        "message_id": message_id, "content": text, "emotes": [], "created_at": "2026-11-21T12:00:00Z", "replies_to": None,
        "broadcaster": {"is_anonymous": False, "user_id": broadcaster_id, "username": "StealthModeGG", "channel_slug": "stealthmodegg"},
        "sender": {"is_anonymous": False, "user_id": user_id, "username": username, "is_verified": False, "channel_slug": username.lower(),
                   "identity": {"username_color": "#fff", "badges": []}},
    }


# ---------------------------------------------------------------------------- fake Kick
class FakeKick:
    """id.kick.com (token endpoint) and api.kick.com in one MockTransport handler. Failures are configured per test."""

    def __init__(self, key: TestKey | None = None) -> None:
        self.key = key or TestKey()
        self.requests: list[tuple[str, str]] = []
        self.sent: list[dict[str, Any]] = []
        self.subscriptions: list[dict[str, Any]] = []
        self.token_calls: list[dict[str, str]] = []
        self.me = {"user_id": 777, "name": "StealthModeGG"}
        self.channel = {"broadcaster_user_id": 777, "slug": "stealthmodegg", "stream": {"is_live": True}}
        self.public_key_style = "json"  # json | pem
        self.reject_tokens: set[str] = set()  # access tokens that answer 401
        self.refresh_fails: int | None = None  # HTTP status for the refresh grant, e.g. 400
        self.fail: dict[str, int] = {}  # "METHOD /path" -> status code
        self.subscribe_error: str | None = None
        self.chat_sent_flag = True
        self._tokens = 0
        self._ids = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.requests.append((method, path))
        if request.url.host == "id.kick.com":
            return self._oauth(request)
        status = self.fail.get(f"{method} {path}")
        if status:
            return httpx.Response(status, json={"message": "injected failure"}, headers={"Retry-After": "7"} if status == 429 else {})
        if path == "/public/v1/public-key":
            if self.public_key_style == "pem":
                return httpx.Response(200, text=self.key.spki_pem())
            return httpx.Response(200, json={"data": {"public_key": self.key.spki_pem()}, "message": "OK"})
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or token in self.reject_tokens:
            return httpx.Response(401, json={"message": "invalid token"})
        if path == "/public/v1/users" and method == "GET":
            return httpx.Response(200, json={"data": [self.me], "message": "OK"})
        if path == "/public/v1/channels" and method == "GET":
            return httpx.Response(200, json={"data": [self.channel], "message": "OK"})
        if path == "/public/v1/events/subscriptions":
            if method == "GET":
                return httpx.Response(200, json={"data": list(self.subscriptions), "message": "OK"})
            if method == "POST":
                body = json.loads(request.content)
                results = []
                for ev in body["events"]:
                    if self.subscribe_error:
                        results.append({"name": ev["name"], "version": ev["version"], "error": self.subscribe_error})
                        continue
                    self._ids += 1
                    sid = f"sub-{self._ids}"
                    self.subscriptions.append({"id": sid, "event": ev["name"], "version": ev["version"], "method": "webhook",
                                               "broadcaster_user_id": 777})
                    results.append({"name": ev["name"], "version": ev["version"], "subscription_id": sid})
                return httpx.Response(200, json={"data": results, "message": "OK"})
            if method == "DELETE":
                ids = request.url.params.get_list("id")
                self.subscriptions = [s for s in self.subscriptions if s["id"] not in ids]
                return httpx.Response(204)
        if path == "/public/v1/chat" and method == "POST":
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(200, json={"data": {"is_sent": self.chat_sent_flag, "message_id": f"msg-{len(self.sent):08d}-aaaa-bbbb-cccc-dddddddddddd"},
                                             "message": "OK"})
        return httpx.Response(404, json={"message": "unknown"})

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode())) if request.method == "POST" and request.content else {}
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200, json={})
        self.token_calls.append(form)
        if form.get("grant_type") == "refresh_token" and self.refresh_fails:
            return httpx.Response(self.refresh_fails, json={"error": "Invalid request"})
        self._tokens += 1
        return httpx.Response(200, json={"access_token": f"access-{self._tokens}", "token_type": "Bearer",
                                         "refresh_token": f"refresh-{self._tokens}", "expires_in": 7200,
                                         "scope": "user:read channel:read chat:write events:subscribe"})
