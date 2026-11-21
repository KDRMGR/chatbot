"""Kick connector: signature verification, the webhook receiver, OAuth, the API client and the adapter, against a fake Kick."""
import asyncio
import socket
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.chat.models import EventType, Platform
from app.config import Settings
from app.platforms.base import ConnectionState
from app.platforms.kick import api as kick_api
from app.platforms.kick import oauth as kick_oauth
from app.platforms.kick.adapter import KickAdapter
from app.platforms.kick.api import KickApi
from app.platforms.kick.credentials import KickAuth, NotAuthorized, ReauthRequired
from app.platforms.kick.normalizer import normalize
from app.platforms.kick.rsa_verify import KeyError_, parse_public_key, verify_pkcs1_v15_sha256
from app.platforms.kick.webhook import WebhookRejected, WebhookServer, WebhookVerifier, build_webhook_app
from app.platforms.youtube.oauth import ClientConfig, TokenSet
from app.security.credential_store import CredentialStore
from tests.kick_fakes import FakeKick, TestKey, chat_payload, signed_headers
from tests.youtube_fakes import Sleeper, eventually

KEY = TestKey()
OTHER_KEY = TestKey(seed=2)


def ts(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def body_of(payload: dict) -> bytes:
    import json

    return json.dumps(payload).encode()


# ---------------------------------------------------------------------------- RSA verification
def test_a_valid_signature_verifies_and_tampering_does_not():
    key = parse_public_key(KEY.spki_pem())
    sig = KEY.sign(b"id.time.{}")
    assert verify_pkcs1_v15_sha256(key, b"id.time.{}", sig)
    assert not verify_pkcs1_v15_sha256(key, b"id.time.{ }", sig)  # one byte different
    assert not verify_pkcs1_v15_sha256(parse_public_key(OTHER_KEY.spki_pem()), b"id.time.{}", sig)  # someone else's key


def test_signatures_of_the_wrong_size_or_value_are_rejected():
    key = parse_public_key(KEY.spki_pem())
    sig = KEY.sign(b"m")
    assert not verify_pkcs1_v15_sha256(key, b"m", sig[:-1]) and not verify_pkcs1_v15_sha256(key, b"m", sig + b"\x00")
    assert not verify_pkcs1_v15_sha256(key, b"m", b"") and not verify_pkcs1_v15_sha256(key, b"m", b"\xff" * len(sig))  # >= modulus
    assert not verify_pkcs1_v15_sha256(key, b"m", b"\x00" * len(sig))


def test_a_second_encoding_of_the_same_signature_is_not_accepted():
    """s and s + n are the same number modulo n. RFC 8017 requires rejecting s >= n, or one valid signature has many spellings."""
    key = TestKey(bits=1020, seed=5)  # n < 2**1020 so that s + n still fits in the same number of bytes
    pair = (key.n, key.e)
    sig = key.sign(b"message")
    assert verify_pkcs1_v15_sha256(pair, b"message", sig)
    twin = (int.from_bytes(sig, "big") + key.n).to_bytes(key.k, "big")
    assert not verify_pkcs1_v15_sha256(pair, b"message", twin)


def test_a_signature_with_sloppy_padding_is_not_accepted():
    """The classic forgery bug: a verifier that PARSES the padded block lets junk hide in it. This one rebuilds and compares."""
    import hashlib

    key = parse_public_key(KEY.spki_pem())
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(b"m").digest()
    for block in (
        b"\x00\x01" + b"\xff" * (KEY.k - len(digest_info) - 4) + b"\x00" + digest_info + b"\x00",  # extra trailing byte
        b"\x00\x01" + b"\xff" * (KEY.k - len(digest_info) - 3) + b"\x00" + digest_info[:-1] + b"\x00",  # damaged hash
        b"\x00\x02" + b"\xff" * (KEY.k - len(digest_info) - 3) + b"\x00" + digest_info,  # wrong block type
        b"\x00\x01" + b"\xff" * 3 + b"\x00" * (KEY.k - len(digest_info) - 6) + b"\x00" + digest_info,  # short padding
    ):
        forged = pow(int.from_bytes(block[: KEY.k].rjust(KEY.k, b"\x00"), "big"), KEY.d, KEY.n).to_bytes(KEY.k, "big")
        assert not verify_pkcs1_v15_sha256(key, b"m", forged)


def test_both_pem_formats_parse_and_junk_is_refused():
    import base64

    assert parse_public_key(KEY.spki_pem()) == (KEY.n, KEY.e) == parse_public_key(KEY.pkcs1_pem())
    der = base64.b64decode("".join(KEY.spki_pem().splitlines()[1:-1]))

    def pem(raw: bytes) -> str:
        return "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(raw).decode() + "\n-----END PUBLIC KEY-----\n"

    for bad in ["", "not a key", "-----BEGIN PUBLIC KEY-----\n!!!!\n-----END PUBLIC KEY-----", KEY.spki_pem()[:-60],
                pem(der[:-5]),  # cut off: the declared lengths run past the data
                pem(der + b"\x00"),  # trailing garbage after the key
                pem(b"\x30\x81" + bytes([len(der) + 10]) + der[3:]),  # length larger than the data
                pem(der.replace(bytes.fromhex("2a864886f70d010101"), bytes.fromhex("2a864886f70d010102"))),  # not the RSA algorithm id
                pem(b"")]:
        with pytest.raises(KeyError_):
            parse_public_key(bad)


def test_weak_keys_are_refused():
    with pytest.raises(KeyError_):
        parse_public_key(TestKey(bits=512, seed=3).spki_pem())  # a 512-bit modulus is breakable: refuse it


# ---------------------------------------------------------------------------- the verifier
def make_verifier(key=KEY, **kw) -> tuple[WebhookVerifier, list]:
    fetched: list = []

    async def fetch():
        fetched.append(1)
        return key.spki_pem()

    return WebhookVerifier(fetch, **kw), fetched


async def test_a_valid_delivery_is_accepted_once():
    verifier, fetched = make_verifier()
    body = body_of(chat_payload())
    headers = signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", ts(), body)
    event, is_new = await verifier.verify(headers, body)
    assert is_new and event.event_type == "chat.message.sent" and event.payload["content"] == "hello everyone!"
    again, is_new2 = await verifier.verify(headers, body)  # Kick retries: same message id
    assert not is_new2 and again.message_id == event.message_id
    assert verifier.counters["accepted"] == 1 and verifier.counters["duplicates"] == 1 and len(fetched) == 1


@pytest.mark.parametrize("break_it, status", [
    (lambda h, b: (h | {"Kick-Event-Signature": ""}, b), 400),
    (lambda h, b: ({k: v for k, v in h.items() if k != "Kick-Event-Message-Timestamp"}, b), 400),
    (lambda h, b: (h | {"Kick-Event-Signature": "***not base64***"}, b), 400),
    (lambda h, b: (h | {"Kick-Event-Message-Timestamp": "yesterday"}, b), 400),
    (lambda h, b: (h | {"Kick-Event-Message-Id": "x"}, b), 400),
    (lambda h, b: (h | {"Kick-Event-Message-Id": "bad id with spaces"}, b), 400),
    (lambda h, b: (h, b + b" "), 401),  # body changed after signing
    (lambda h, b: (h | {"Kick-Event-Signature": OTHER_KEY and signed_headers(OTHER_KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", h["Kick-Event-Message-Timestamp"], b)["Kick-Event-Signature"]}, b), 401),
])
async def test_bad_deliveries_are_refused_with_the_right_status(break_it, status):
    verifier, _ = make_verifier()
    body = body_of(chat_payload())
    headers, body2 = break_it(signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", ts(), body), body)
    with pytest.raises(WebhookRejected) as err:
        await verifier.verify(headers, body2)
    assert err.value.status == status and verifier.counters["rejected"] == 1


async def test_old_and_future_timestamps_are_refused_even_with_a_valid_signature():
    verifier, _ = make_verifier(tolerance_s=300)
    body = body_of(chat_payload())
    for offset in (-3600, 3600):
        headers = signed_headers(KEY, f"01HZZZZZZZZZZZZZZZZZZZZZ{abs(offset)}", ts(offset), body)
        with pytest.raises(WebhookRejected) as err:
            await verifier.verify(headers, body)
        assert err.value.status == 401


async def test_signed_but_unusable_bodies_are_refused():
    verifier, _ = make_verifier()
    for i, raw in enumerate([b"not json at all", b"[1, 2, 3]", b""]):
        with pytest.raises(WebhookRejected) as err:
            await verifier.verify(signed_headers(KEY, f"01HZZZZZZZZZZZZZZZZZZZZZZ{i}", ts(), raw), raw)
        assert err.value.status == 400


async def test_a_rotated_key_is_picked_up_once_and_bad_signatures_cannot_make_us_hammer_kick():
    keys = [KEY, OTHER_KEY]  # Kick rotates: the first fetch returns the old key, later ones the new key
    fetches = []
    offset = [0.0]

    async def fetch():
        fetches.append(1)
        return keys[min(len(fetches) - 1, 1)].spki_pem()

    verifier = WebhookVerifier(fetch, clock=lambda: time.time() + offset[0], refetch_min_interval_s=60)
    body = body_of(chat_payload())
    old = signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", ts(), body)
    await verifier.verify(old, body)  # cached old key
    offset[0] += 100  # a minute and a half later (inside the replay window): Kick has rotated its key
    fresh = signed_headers(OTHER_KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ2", ts(), body)
    event, is_new = await verifier.verify(fresh, body)  # fails with the old key, re-fetches once, succeeds
    assert is_new and len(fetches) == 2
    # an attacker spraying forged signatures: no extra fetches inside the minimum interval
    for i in range(20):
        with pytest.raises(WebhookRejected):
            await verifier.verify(signed_headers(TestKey(seed=9), f"01HZZZZZZZZZZZZZZZZZZZZZ{i:02d}", ts(), body), body)
    assert len(fetches) == 2


async def test_failing_to_fetch_the_key_is_a_503_so_kick_retries():
    async def fetch():
        raise RuntimeError("kick unreachable")

    verifier = WebhookVerifier(fetch)
    body = body_of(chat_payload())
    with pytest.raises(WebhookRejected) as err:
        await verifier.verify(signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", ts(), body), body)
    assert err.value.status == 503


async def test_the_replay_memory_is_bounded():
    verifier, _ = make_verifier(seen_max=5)
    body = body_of(chat_payload())
    for i in range(20):
        await verifier.verify(signed_headers(KEY, f"01HZZZZZZZZZZZZZZZZZZZZ{i:03d}", ts(), body), body)
    assert len(verifier._seen) == 5 and len(verifier._seen_order) == 5


# ---------------------------------------------------------------------------- the webhook HTTP app
async def webhook_client(on_event=None, verifier=None, **kw):
    received: list = []

    async def default(event):
        received.append(event)

    app = build_webhook_app(verifier or make_verifier()[0], on_event or default, **kw)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kick.test"), received


async def test_the_webhook_route_verifies_acknowledges_and_hands_over_the_event():
    client, received = await webhook_client()
    body = body_of(chat_payload())
    ok = await client.post("/kick/webhook", content=body, headers=signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", ts(), body))
    assert ok.status_code == 200 and len(received) == 1
    dup = await client.post("/kick/webhook", content=body, headers=signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", ts(), body))
    assert dup.status_code == 200 and len(received) == 1  # acknowledged, not processed twice
    bad = await client.post("/kick/webhook", content=body, headers=signed_headers(OTHER_KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ2", ts(), body))
    assert bad.status_code == 401 and len(received) == 1 and "hello" not in bad.text  # never echoes request data


async def test_the_webhook_app_exposes_nothing_else():
    client, _ = await webhook_client()
    assert (await client.get("/kick/health")).text.startswith("stealthbot kick webhook")
    for path in ["/api/status", "/dashboard", "/", "/kick/webhook/../api/status", "/api/health"]:
        assert (await client.get(path)).status_code in (404, 405), path
    assert (await client.get("/kick/webhook")).status_code == 405
    assert (await client.put("/kick/webhook", content=b"{}")).status_code == 405


async def test_oversized_bodies_are_refused_before_any_work():
    client, received = await webhook_client(max_body=2000)
    big = b"x" * 5000
    assert (await client.post("/kick/webhook", content=big, headers={"Content-Length": "5000"})).status_code == 413
    assert (await client.post("/kick/webhook", content=big)).status_code == 413
    assert (await client.post("/kick/webhook", content=b"{}", headers={"Content-Length": "abc"})).status_code == 413

    async def endless():  # no Content-Length at all (chunked): the size must be enforced while reading
        for _ in range(10):
            yield b"x" * 500

    assert (await client.post("/kick/webhook", content=endless())).status_code == 413
    assert received == []


async def test_our_own_trouble_does_not_turn_a_valid_delivery_into_an_error():
    async def broken(event):
        raise RuntimeError("database is down")

    client, _ = await webhook_client(on_event=broken)
    body = body_of(chat_payload())
    out = await client.post("/kick/webhook", content=body, headers=signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZZ1", ts(), body))
    assert out.status_code == 200  # Kick would unsubscribe a webhook that keeps failing


async def test_the_embedded_server_listens_on_loopback_only_and_a_taken_port_is_an_error_not_a_crash():
    app = build_webhook_app(make_verifier()[0], lambda e: asyncio.sleep(0))
    server = WebhookServer(app, 0)
    await server.start()
    try:
        assert server.running and server._sock.getsockname()[0] == "127.0.0.1" and server.port > 0
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"http://127.0.0.1:{server.port}/kick/health")).status_code == 200
        second = WebhookServer(app, server.port)
        with pytest.raises(OSError):
            await second.start()  # port taken: an ordinary error, the process keeps running
    finally:
        await server.stop()
    assert not server.running
    with socket.socket() as probe:  # the port is free again
        probe.bind(("127.0.0.1", server.port))


# ---------------------------------------------------------------------------- normalizer
def test_chat_payload_becomes_a_chat_message():
    msg = normalize("chat.message.sent", chat_payload("what game is this?"), "01HULID", own_user_ids=[999])
    assert (msg.platform, msg.viewer_id, msg.username, msg.message) == (Platform.KICK, "4242", "Alice", "what game is this?")
    assert msg.event_type is EventType.MESSAGE and msg.message_id == "11111111-2222-3333-4444-555555555555"
    assert not msg.is_bot_self and not msg.is_streamer and msg.timestamp.year == 2026


def test_own_messages_and_the_streamer_are_flagged():
    assert normalize("chat.message.sent", chat_payload(user_id=999), "x", own_user_ids=[999]).is_bot_self
    assert normalize("chat.message.sent", chat_payload(user_id=777, broadcaster_id=777), "x").is_streamer


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(sender={"is_anonymous": True, "user_id": 1, "username": "x"}),
    lambda p: p.update(sender=None), lambda p: p.pop("content"), lambda p: p.pop("message_id"),
    lambda p: p.update(content=12345),
])
def test_malformed_chat_payloads_are_dropped_not_crashed_on(mutate):
    payload = chat_payload()
    mutate(payload)
    assert normalize("chat.message.sent", payload, "x") is None


def test_other_events_map_to_donations_and_memberships_and_unknown_ones_are_ignored():
    sub = normalize("channel.subscription.new", {"subscriber": {"user_id": 5, "username": "Bob"}, "created_at": "2026-11-21T12:00:00Z"}, "01H1")
    assert sub.event_type is EventType.MEMBERSHIP and sub.username == "Bob" and sub.message_id == "channel.subscription.new:01H1"
    gifts = normalize("channel.subscription.gifts", {"gifter": {"user_id": 6, "username": "Gina"}, "giftees": [{}, {}, {}]}, "01H2")
    assert gifts.event_type is EventType.MEMBERSHIP and gifts.amount == "3"
    anon = normalize("channel.subscription.gifts", {"gifter": {"is_anonymous": True}, "giftees": []}, "01H3")
    assert anon.username == "Someone"
    kicks = normalize("kicks.gifted", {"sender": {"user_id": 7, "username": "Kim"}, "gift": {"amount": 50, "message": "gg"}}, "01H4")
    assert kicks.event_type is EventType.DONATION and kicks.amount == "50 KICKs" and kicks.message == "gg"
    for ignored in ["channel.followed", "livestream.status.updated", "moderation.banned", "made.up"]:
        assert normalize(ignored, {"follower": {"user_id": 1}}, "x") is None


def test_a_very_long_event_id_is_hashed_to_fit_the_database():
    msg = normalize("channel.subscription.new", {"subscriber": {"user_id": 5, "username": "Bob"}}, "E" * 300)
    assert len(msg.message_id) <= 128 and msg.message_id.startswith("h:")


# ---------------------------------------------------------------------------- OAuth
def test_the_authorisation_url_has_pkce_state_and_all_scopes():
    verifier, challenge = kick_oauth.generate_pkce()
    url = kick_oauth.build_auth_url(ClientConfig("cid", "sec"), "http://localhost:8789/callback", kick_oauth.SCOPES, "st8", challenge)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://id.kick.com/oauth/authorize?") and q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"] == [challenge] and q["state"] == ["st8"]
    assert q["redirect_uri"] == ["http://localhost:8789/callback"] and q["client_id"] == ["cid"]
    assert set(q["scope"][0].split()) == {"user:read", "channel:read", "chat:write", "events:subscribe"}
    assert "secret" not in url and verifier not in url


async def test_the_code_exchange_and_refresh_send_what_kicks_docs_require():
    fake = FakeKick()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        tokens = await kick_oauth.exchange_code(http, ClientConfig("cid", "sec"), "the-code", "the-verifier", "http://localhost:8789/callback")
        assert fake.token_calls[0] == {"grant_type": "authorization_code", "client_id": "cid", "client_secret": "sec", "code": "the-code",
                                       "code_verifier": "the-verifier", "redirect_uri": "http://localhost:8789/callback"}
        assert tokens.access_token == "access-1" and tokens.refresh_token == "refresh-1" and tokens.scope.startswith("user:read")
        fresh = await kick_oauth.refresh_tokens(http, ClientConfig("cid", "sec"), tokens.refresh_token)
        assert fake.token_calls[1] == {"grant_type": "refresh_token", "client_id": "cid", "client_secret": "sec", "refresh_token": "refresh-1"}
        assert fresh.access_token == "access-2"
        assert "access-1" not in repr(tokens) and "refresh-1" not in repr(tokens)  # never printed


async def test_kick_requires_the_client_secret_and_a_dead_refresh_means_sign_in_again():
    fake = FakeKick()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        with pytest.raises(kick_oauth.KickOAuthError) as err:
            await kick_oauth.exchange_code(http, ClientConfig("cid", None), "c", "v", "http://localhost:8789/callback")
        assert err.value.error == "missing_client_secret" and fake.token_calls == []
        for status, reauth in [(400, True), (401, True), (500, False), (503, False)]:
            fake.refresh_fails = status
            with pytest.raises(kick_oauth.KickOAuthError) as err:
                await kick_oauth.refresh_tokens(http, ClientConfig("cid", "sec"), "r")
            assert err.value.needs_reauth is reauth, status


async def test_a_network_failure_is_not_a_reason_to_sign_in_again():
    def boom(request):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        with pytest.raises(kick_oauth.KickOAuthError) as err:
            await kick_oauth.refresh_tokens(http, ClientConfig("cid", "sec"), "r")
    assert err.value.error == "network_error" and not err.value.needs_reauth


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_the_loopback_sign_in_end_to_end_rejects_forged_requests():
    fake = FakeKick()
    port = free_port()
    seen: dict = {}

    def opener(url: str) -> bool:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        seen.update(q)

        async def browser():
            async with httpx.AsyncClient() as client:
                base = q["redirect_uri"][0]
                forged = await client.get(base, params={"code": "evil", "state": "wrong"})  # another page / process
                seen["forged_status"] = forged.status_code
                good = await client.get(base, params={"code": "good-code", "state": q["state"][0]})
                seen["good_status"] = good.status_code

        seen["task"] = asyncio.get_running_loop().create_task(browser())
        return True

    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
        tokens = await kick_oauth.run_loopback_login(http, ClientConfig("cid", "sec"), port=port, opener=opener,
                                                     announce=lambda _: None, timeout=10)
    await seen["task"]  # let the fake browser finish reading its answer
    assert seen["redirect_uri"] == [f"http://localhost:{port}/callback"]
    assert seen["forged_status"] == 400 and seen["good_status"] == 200
    assert tokens.access_token == "access-1" and fake.token_calls[0]["code"] == "good-code"
    assert fake.token_calls[0]["redirect_uri"] == f"http://localhost:{port}/callback"


async def test_sign_in_on_a_taken_port_is_a_clear_error():
    fake = FakeKick()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as http:
            with pytest.raises(kick_oauth.KickOAuthError) as err:
                await kick_oauth.run_loopback_login(http, ClientConfig("cid", "sec"), port=port, opener=lambda u: True,
                                                    announce=lambda _: None, timeout=1)
    assert err.value.error == "port_in_use"


async def test_revoke_sends_the_documented_parameters():
    fake = FakeKick()
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await kick_oauth.revoke_token(http, "tok", "refresh_token") is True
    assert seen == {"token": "tok", "token_hint_type": "refresh_token"}


# ---------------------------------------------------------------------------- the API client
async def make_api(tmp_path, fake=None):
    fake = fake or FakeKick(KEY)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    auth = KickAuth(CredentialStore(tmp_path), http)
    auth.save_login(ClientConfig("cid", "secret"), TokenSet("access-0", "refresh-0", time.time() + 7200,
                                                          scope="user:read channel:read chat:write events:subscribe"))
    return fake, auth, KickApi(auth, http), http


async def test_identity_channel_and_public_key(tmp_path):
    fake, auth, api, http = await make_api(tmp_path)
    assert await api.me() == {"user_id": 777, "name": "StealthModeGG"}
    assert await api.channel() == {"broadcaster_user_id": 777, "slug": "stealthmodegg", "is_live": True}
    assert "BEGIN PUBLIC KEY" in await api.public_key()
    fake.public_key_style = "pem"  # the docs show a raw PEM answer too: both are accepted
    assert "BEGIN PUBLIC KEY" in await api.public_key()
    assert ("GET", "/public/v1/users") in fake.requests
    await http.aclose()


async def test_a_rejected_token_is_refreshed_once_and_the_request_retried(tmp_path):
    fake, auth, api, http = await make_api(tmp_path)
    fake.reject_tokens = {"access-0"}
    assert (await api.me())["user_id"] == 777
    assert [c["grant_type"] for c in fake.token_calls] == ["refresh_token"]
    fake.reject_tokens = {"access-1", "access-2"}  # the stored token AND the next refreshed one are refused
    with pytest.raises(kick_api.AuthError):
        await api.me()
    await http.aclose()


async def test_a_dead_refresh_token_raises_reauth_and_http_errors_are_classified(tmp_path):
    fake, auth, api, http = await make_api(tmp_path)
    fake.reject_tokens, fake.refresh_fails = {"access-0"}, 400
    with pytest.raises(ReauthRequired):  # the adapter turns this into "run scripts\kick_login.py"
        await api.me()
    assert auth.reauth_required
    fake.reject_tokens, fake.refresh_fails = set(), None
    cases = {403: kick_api.Forbidden, 404: kick_api.NotFound, 400: kick_api.BadRequest, 500: kick_api.ServerError, 429: kick_api.RateLimited}
    for status, exc in cases.items():
        fake.fail = {"GET /public/v1/users": status}
        with pytest.raises(exc) as err:
            await api.me()
        assert err.value.status == status and "access-" not in str(err.value)  # no token in error text
    fake.fail = {"GET /public/v1/users": 429}
    with pytest.raises(kick_api.RateLimited) as err:
        await api.me()
    assert err.value.retry_after == 7.0
    await http.aclose()


async def test_not_signed_in_is_reported_without_touching_the_network(tmp_path):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("no request expected")))
    api = KickApi(KickAuth(CredentialStore(tmp_path), http), http)
    with pytest.raises(NotAuthorized):
        await api.me()
    await http.aclose()


async def test_subscriptions_are_listed_created_and_removed(tmp_path):
    fake, auth, api, http = await make_api(tmp_path)
    assert await api.list_subscriptions() == []
    results = await api.subscribe([("chat.message.sent", 1)])
    assert results[0]["subscription_id"] == "sub-1" and fake.subscriptions[0]["event"] == "chat.message.sent"
    await api.unsubscribe(["sub-1"])
    assert fake.subscriptions == []
    await http.aclose()


async def test_sending_as_bot_or_user_follows_the_documented_body(tmp_path):
    fake, auth, api, http = await make_api(tmp_path)
    bot = await api.send_chat("hi there", kind="bot", reply_to="3f8c0f4e-0000-0000-0000-000000000001")
    assert bot["is_sent"] and fake.sent[0] == {"content": "hi there", "type": "bot", "reply_to_message_id": "3f8c0f4e-0000-0000-0000-000000000001"}
    await api.send_chat("hi again", kind="user", broadcaster_user_id=777)
    assert fake.sent[1] == {"content": "hi again", "type": "user", "broadcaster_user_id": 777}
    with pytest.raises(ValueError):
        await api.send_chat("x", kind="user")  # a message sent as 'user' needs the channel id
    with pytest.raises(ValueError):
        await api.send_chat("x", kind="admin")
    await http.aclose()


async def test_long_messages_respect_both_documented_limits(tmp_path):
    fake, auth, api, http = await make_api(tmp_path)
    await api.send_chat("a" * 900)
    assert len(fake.sent[-1]["content"]) == 500
    await api.send_chat("😀" * 600)  # 4 bytes each: 500 characters would be 2000 bytes, but never above 2048
    assert len(fake.sent[-1]["content"].encode()) <= 2048 and len(fake.sent[-1]["content"]) <= 500
    fake.chat_sent_flag = False
    assert (await api.send_chat("hello"))["is_sent"] is False
    await http.aclose()


async def test_logout_revokes_and_forgets(tmp_path):
    fake, auth, api, http = await make_api(tmp_path)
    assert await auth.revoke_and_forget() is True
    assert not auth.is_configured()
    with pytest.raises(NotAuthorized):
        await api.me()
    await http.aclose()


# ---------------------------------------------------------------------------- the adapter
class Rig:
    def __init__(self, tmp_path, fake, auth, api, http, adapter, received, sleeper, settings):
        self.tmp_path, self.fake, self.auth, self.api, self.http = tmp_path, fake, auth, api, http
        self.adapter, self.received, self.sleeper, self.settings = adapter, received, sleeper, settings

    @property
    def port(self) -> int:
        return self.adapter._server.port

    async def deliver(self, payload=None, *, key=None, event_type="chat.message.sent", message_id=None, ts_=None, raw=None):
        payload = payload if payload is not None else chat_payload()
        body = raw if raw is not None else body_of(payload)
        mid = message_id or f"01HZ{int(time.time() * 1e6) % 10**20:020d}"
        headers = signed_headers(key or self.fake.key, mid, ts_ or ts(), body, event_type)
        async with httpx.AsyncClient() as client:
            return await client.post(f"http://127.0.0.1:{self.port}/kick/webhook", content=body, headers=headers)


async def make_rig(tmp_path, *, signed_in=True, start=True, fake=None, **overrides):
    fake = fake or FakeKick(KEY)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    auth = KickAuth(CredentialStore(tmp_path), http)
    if signed_in:
        auth.save_login(ClientConfig("cid", "secret"), TokenSet("access-0", "refresh-0", time.time() + 7200,
                                                              scope="user:read channel:read chat:write events:subscribe"))
    api = KickApi(auth, http)
    settings = Settings(state_dir=tmp_path, **{"kick_enabled": True, "kick_webhook_port": 0, "kick_resubscribe_interval_s": 0.05, **overrides})
    received: list = []

    async def on_message(msg):
        received.append(msg)

    logged: list = []

    async def log_event(kind, message):
        logged.append((kind, message))

    sleeper = Sleeper(real=0.02)
    adapter = KickAdapter(settings, api, auth, log_event=log_event, sleep=sleeper)
    rig = Rig(tmp_path, fake, auth, api, http, adapter, received, sleeper, settings)
    rig.logged = logged
    if start:
        await adapter.start(on_message)
    rig.on_message = on_message
    return rig


async def close(rig: Rig):
    await rig.adapter.stop()
    await rig.http.aclose()


async def connected(rig: Rig):
    await eventually(lambda: rig.adapter.status().state is ConnectionState.CONNECTED, timeout=10)


async def test_it_connects_subscribes_and_reports_how_to_check_the_tunnel(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    assert [s["event"] for s in rig.fake.subscriptions] == ["chat.message.sent"]
    detail = rig.adapter.status().detail
    assert "stealthmodegg" in detail and f"127.0.0.1:{rig.port}" in detail and "waiting for the first event" in detail
    info = rig.adapter.details()
    assert info["channel"] == "stealthmodegg" and info["is_live"] is True and info["subscribed_events"] == ["chat.message.sent"]
    assert info["message_type"] == "bot" and info["webhook_port"] == rig.port and "chat:write" in info["permission"]
    await close(rig)
    assert rig.adapter.status().state is ConnectionState.DISCONNECTED


async def test_a_signed_chat_delivery_reaches_the_pipeline(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    assert (await rig.deliver(chat_payload("hello from kick"))).status_code == 200
    await eventually(lambda: rig.received)
    msg = rig.received[0]
    assert msg.platform is Platform.KICK and msg.message == "hello from kick" and msg.viewer_id == "4242" and not msg.is_bot_self
    assert rig.adapter.details()["last_event_s_ago"] is not None
    await close(rig)


async def test_chat_from_another_channel_is_counted_but_never_answered(tmp_path):
    """The app can be subscribed to a channel that is not ours (an experiment, an old subscription). Our replies go to OUR channel,
    so answering that chat would put the answer in the wrong place."""
    rig = await make_rig(tmp_path)
    await connected(rig)
    assert (await rig.deliver(chat_payload("hi from somewhere else", broadcaster_id=999))).status_code == 200
    assert (await rig.deliver(chat_payload("hi from our own chat", broadcaster_id=777, message_id="aaaaaaaa-2222-3333-4444-555555555555"))).status_code == 200
    await eventually(lambda: rig.received)
    await asyncio.sleep(0.2)
    assert [m.message for m in rig.received] == ["hi from our own chat"]
    info = rig.adapter.details()
    assert info["events"] == 2 and info["other_channel_events"] == 1 and info["ignored_events"] == 1  # delivery is still visible
    await close(rig)


async def test_it_chats_in_another_channel_reading_through_the_app_token_and_posting_as_the_account(tmp_path):
    """The bot account (signed in) chats in a DIFFERENT channel: chat is read through a subscription made with the APP token,
    answers are normal user messages addressed to that channel, and the bot's own messages are recognised by sender id."""
    rig = await make_rig(tmp_path, kick_target_channel="otherchannel")
    await connected(rig)
    assert [(s["broadcaster_user_id"], s["event"]) for s in rig.fake.subscriptions] == [(555, "chat.message.sent")]  # not our own channel
    assert any(c.get("grant_type") == "client_credentials" for c in rig.fake.token_calls)
    info = rig.adapter.details()
    assert info["channel"] == "otherchannel" and info["own_channel"] == "stealthmodegg" and info["target_channel"] == "otherchannel"
    assert info["answering_in_own_channel"] is False and info["is_live"] is True
    assert "chatting in otherchannel" in rig.adapter.status().detail and "signed in as StealthModeGG" in rig.adapter.status().detail

    assert (await rig.deliver(chat_payload("hello from the other chat", broadcaster_id=555))).status_code == 200
    assert (await rig.deliver(chat_payload("hello from our own chat", broadcaster_id=777, message_id="bbbbbbbb-2222-3333-4444-555555555555"))).status_code == 200
    await eventually(lambda: rig.received)
    await asyncio.sleep(0.2)
    assert [m.message for m in rig.received] == ["hello from the other chat"]  # our own channel is the OTHER one now
    assert rig.adapter.details()["other_channel_events"] == 1

    result = await rig.adapter.send("Hey chat!")
    assert result.ok
    assert rig.fake.sent[-1] == {"content": "Hey chat!", "type": "user", "broadcaster_user_id": 555}  # never type 'bot' (own channel only)
    assert rig.fake.sent_with == ["user"]  # chat:write only works with the signed-in account's token, never the app token

    # the bot's message comes back as a chat event from OUR account in that channel: it must not be answered
    await rig.deliver(chat_payload("Hey chat!", user_id=777, username="StealthModeGG", broadcaster_id=555,
                                   message_id="cccccccc-2222-3333-4444-555555555555"))
    await eventually(lambda: len(rig.received) == 2)
    assert rig.received[1].is_bot_self is True
    await close(rig)


async def test_an_existing_subscription_for_the_other_channel_is_reused_after_a_restart(tmp_path):
    rig = await make_rig(tmp_path, kick_target_channel="otherchannel")
    await connected(rig)
    posts_before = sum(1 for m, p in rig.fake.requests if m == "POST" and p.endswith("/events/subscriptions"))
    await rig.adapter.stop()
    await rig.adapter.start(rig.on_message)
    await connected(rig)
    posts_after = sum(1 for m, p in rig.fake.requests if m == "POST" and p.endswith("/events/subscriptions"))
    assert posts_before == posts_after == 1 and len(rig.fake.subscriptions) == 1
    await close(rig)


async def test_a_channel_that_does_not_exist_is_reported_by_name_and_nothing_is_subscribed(tmp_path):
    rig = await make_rig(tmp_path, kick_target_channel="nosuchchannel")
    await eventually(lambda: rig.adapter.status().state is ConnectionState.ERROR, timeout=10)
    assert "no channel called 'nosuchchannel'" in rig.adapter.status().detail
    assert rig.fake.subscriptions == []
    await close(rig)


async def test_retarget_moves_between_the_own_channel_and_another_one(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    assert rig.adapter.details()["answering_in_own_channel"] is True
    await rig.adapter.retarget("OtherChannel")  # case does not matter
    await connected(rig)
    assert rig.adapter.details()["channel"] == "otherchannel" and rig.adapter.details()["last_event_s_ago"] is None
    assert {s["broadcaster_user_id"] for s in rig.fake.subscriptions} == {777, 555}  # the own one stays (harmless: ignored)
    await rig.adapter.retarget(None)
    await connected(rig)
    assert rig.adapter.details()["channel"] == "stealthmodegg" and rig.adapter.details()["answering_in_own_channel"] is True
    assert (await rig.adapter.send("back home")).ok and rig.fake.sent[-1]["type"] == "bot"
    await rig.adapter.stop()  # the streamer pressed Disconnect
    await rig.adapter.retarget("otherchannel")
    assert rig.adapter.status().state is ConnectionState.DISCONNECTED and rig.adapter._task is None  # stays stopped
    await close(rig)


async def test_the_app_token_is_cached_renewed_after_a_401_and_never_used_for_the_accounts_own_calls(tmp_path):
    rig = await make_rig(tmp_path, kick_target_channel="otherchannel")
    await connected(rig)
    grants = lambda: sum(1 for c in rig.fake.token_calls if c.get("grant_type") == "client_credentials")  # noqa: E731
    assert grants() == 1
    await rig.api.list_subscriptions(app=True)
    await rig.api.list_subscriptions(app=True)
    assert grants() == 1  # cached
    rig.fake.reject_tokens.add("app-1")
    rows = await rig.api.list_subscriptions(app=True)  # Kick rejects the cached token: one new one is requested and the call retried
    assert grants() == 2 and rows
    await rig.api.me()  # the account's own calls use the account's token
    assert grants() == 2
    await close(rig)


async def test_channel_lookup_by_name_is_batched_validated_and_tolerant(tmp_path):
    rig = await make_rig(tmp_path, start=False)
    found = await rig.api.channels_by_slug(["OtherChannel", "stealthmodegg", "nobody"])
    assert set(found) == {"otherchannel", "stealthmodegg"} and found["otherchannel"]["is_live"] is True
    assert found["otherchannel"]["title"] == "Live now" and found["otherchannel"]["viewers"] == 3
    assert rig.fake.slug_lookups == [["otherchannel", "stealthmodegg", "nobody"]]  # ONE call, names lower-cased
    assert await rig.api.channels_by_slug([]) == {} and await rig.api.channel_by_slug("nobody") is None
    for bad in (["x" * 26], [f"c{n}" for n in range(51)]):
        with pytest.raises(ValueError):
            await rig.api.channels_by_slug(bad)
    with pytest.raises(ValueError, match="needs the broadcaster"):
        await rig.api.subscribe([("chat.message.sent", 1)], app=True)
    await close(rig)


def test_the_target_channel_setting_is_validated_and_normalised():
    assert Settings(kick_target_channel="  StealhModeGG ").kick_target_channel == "stealhmodegg"
    for bad in ["has space", "x" * 26, "-leading", "semi;colon", "https://kick.com/x"]:
        with pytest.raises(ValueError, match="kick_target_channel"):
            Settings(kick_target_channel=bad)


async def test_forged_old_and_repeated_deliveries_are_not_processed(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    assert (await rig.deliver(key=OTHER_KEY)).status_code == 401  # someone who does not have Kick's private key
    assert (await rig.deliver(ts_=ts(-7200))).status_code == 401  # a captured delivery replayed hours later
    ok = await rig.deliver(chat_payload("once"), message_id="01HZZZZZZZZZZZZZZZZZZZZZR1")
    again = await rig.deliver(chat_payload("once"), message_id="01HZZZZZZZZZZZZZZZZZZZZZR1")  # Kick's retry
    assert ok.status_code == again.status_code == 200
    await asyncio.sleep(0.2)
    assert [m.message for m in rig.received] == ["once"]
    await close(rig)


async def test_events_that_were_not_asked_for_or_are_not_versioned_1_are_ignored(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    await rig.deliver({"follower": {"user_id": 1, "username": "x"}}, event_type="channel.followed")
    await rig.deliver({"subscriber": {"user_id": 5, "username": "Bob"}}, event_type="channel.subscription.new")  # not in kick_events
    await asyncio.sleep(0.2)
    assert rig.received == [] and rig.adapter.details()["ignored_events"] == 2
    await close(rig)


async def test_subscription_and_gift_events_can_be_switched_on(tmp_path):
    rig = await make_rig(tmp_path, kick_events=["chat.message.sent", "channel.subscription.new", "kicks.gifted"])
    await connected(rig)
    assert {s["event"] for s in rig.fake.subscriptions} == {"chat.message.sent", "channel.subscription.new", "kicks.gifted"}
    await rig.deliver({"subscriber": {"user_id": 5, "username": "Bob"}}, event_type="channel.subscription.new")
    await rig.deliver({"sender": {"user_id": 7, "username": "Kim"}, "gift": {"amount": 50, "message": "gg"}}, event_type="kicks.gifted")
    await eventually(lambda: len(rig.received) == 2)
    assert [m.event_type for m in rig.received] == [EventType.MEMBERSHIP, EventType.DONATION]
    await close(rig)


async def test_the_webhook_port_serves_nothing_but_the_webhook(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    async with httpx.AsyncClient() as client:
        for path in ["/api/status", "/api/health", "/dashboard", "/"]:
            assert (await client.get(f"http://127.0.0.1:{rig.port}{path}")).status_code == 404, path
    await close(rig)


async def test_sending_posts_as_the_bot_and_its_own_echo_is_ignored(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    out = await rig.adapter.send("Hello chat, I'm an automated assistant.")
    assert out.ok and rig.fake.sent[0] == {"content": "Hello chat, I'm an automated assistant.", "type": "bot"}
    # the webhook for our own message arrives, carrying the id Kick gave us (Kick may have changed the text a little)
    await rig.deliver(chat_payload("[emote:123:wave] Hello chat, welcome in", user_id=31337, message_id=out.platform_message_id))
    await eventually(lambda: rig.received)
    assert rig.received[0].is_bot_self
    await close(rig)


async def test_an_echo_that_beats_the_send_answer_is_still_recognised_by_its_text(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    rig.adapter._sent_texts.append(("hello chat im an automated assistant", rig.adapter._clock()))  # send() registers the text before the call
    await rig.deliver(chat_payload("Hello chat, I'm an automated assistant.", user_id=31337, message_id="never-seen-id-aaaa"))
    await eventually(lambda: rig.received)
    assert rig.received[0].is_bot_self
    await rig.deliver(chat_payload("something else entirely", user_id=31337, message_id="other-id-bbbbbbbb"))
    await eventually(lambda: len(rig.received) == 2)
    assert not rig.received[1].is_bot_self
    await close(rig)


async def test_known_bot_user_ids_are_ignored(tmp_path):
    rig = await make_rig(tmp_path, kick_bot_user_id="31337")
    await connected(rig)
    await rig.deliver(chat_payload("hi", user_id=31337))
    await eventually(lambda: rig.received)
    assert rig.received[0].is_bot_self
    await close(rig)


async def test_sending_as_user_needs_and_sends_the_channel_id_and_threads_are_optional(tmp_path):
    rig = await make_rig(tmp_path, kick_message_type="user", kick_reply_threads=True)
    await connected(rig)
    from app.chat.models import ChatMessage

    asked = ChatMessage(platform=Platform.KICK, viewer_id="1", username="v", message="q?", message_id="3f8c0f4e-0000-0000-0000-000000000009")
    assert (await rig.adapter.send("an answer", reply_to=asked)).ok
    assert rig.fake.sent[0] == {"content": "an answer", "type": "user", "broadcaster_user_id": 777,
                                "reply_to_message_id": "3f8c0f4e-0000-0000-0000-000000000009"}
    await close(rig)


async def test_send_failures_are_reported_with_the_right_retry_hint(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    for status, retryable in [(403, False), (429, True), (500, True), (400, False)]:
        rig.fake.fail = {"POST /public/v1/chat": status}
        out = await rig.adapter.send("hello")
        assert not out.ok and out.retryable is retryable and "access-" not in (out.error or ""), status
    rig.fake.fail = {}
    rig.fake.chat_sent_flag = False
    assert not (await rig.adapter.send("hello")).ok
    assert rig.adapter.details()["sends_failed"] == 5
    await close(rig)
    assert (await rig.adapter.send("after stop")).retryable  # not connected any more: try again later


async def test_without_a_sign_in_it_says_so_and_stays_off_the_network(tmp_path):
    rig = await make_rig(tmp_path, signed_in=False)
    await eventually(lambda: rig.adapter.status().state is ConnectionState.ERROR)
    assert "kick_login" in rig.adapter.status().detail and rig.fake.requests == []
    assert 300 in rig.sleeper.delays  # waits five minutes instead of retrying in a loop
    await close(rig)


async def test_a_sign_in_without_all_permissions_is_reported(tmp_path):
    rig = await make_rig(tmp_path, start=False)
    rig.auth.save_login(ClientConfig("cid", "secret"), TokenSet("access-0", "refresh-0", time.time() + 7200, scope="user:read channel:read"))
    await rig.adapter.start(rig.on_message)
    await eventually(lambda: rig.adapter.status().state is ConnectionState.ERROR)
    assert "chat:write" in rig.adapter.status().detail and "events:subscribe" in rig.adapter.status().detail
    await close(rig)


async def test_a_dead_refresh_token_asks_for_a_new_sign_in(tmp_path):
    fake = FakeKick(KEY)
    fake.reject_tokens, fake.refresh_fails = {"access-0"}, 400
    rig = await make_rig(tmp_path, fake=fake)
    await eventually(lambda: rig.adapter.status().state is ConnectionState.ERROR)
    assert "kick_login" in rig.adapter.status().detail and rig.auth.reauth_required
    await close(rig)


async def test_kick_errors_back_off_and_the_adapter_recovers_on_its_own(tmp_path):
    fake = FakeKick(KEY)
    fake.fail = {"GET /public/v1/users": 500}
    rig = await make_rig(tmp_path, fake=fake)
    await eventually(lambda: rig.adapter.status().state is ConnectionState.ERROR)
    assert "retrying" in rig.adapter.status().detail and rig.sleeper.delays
    fake.fail = {}
    await connected(rig)
    assert rig.adapter.details()["reconnects"] >= 1
    await close(rig)


async def test_rate_limiting_uses_the_wait_kick_asks_for(tmp_path):
    fake = FakeKick(KEY)
    fake.fail = {"GET /public/v1/users": 429}
    rig = await make_rig(tmp_path, fake=fake)
    await eventually(lambda: 7.0 in rig.sleeper.delays)  # Retry-After: 7
    fake.fail = {}
    await connected(rig)
    await close(rig)


async def test_a_refused_subscription_is_an_error_with_kicks_reason(tmp_path):
    fake = FakeKick(KEY)
    fake.subscribe_error = "limit reached"
    rig = await make_rig(tmp_path, fake=fake)
    await eventually(lambda: rig.adapter.status().state is ConnectionState.ERROR)
    assert "limit reached" in rig.adapter.status().detail
    fake.subscribe_error = None
    await connected(rig)
    await close(rig)


async def test_a_subscription_kick_dropped_is_restored_and_logged(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    rig.fake.subscriptions.clear()  # Kick unsubscribed us after a day of failures
    await eventually(lambda: rig.adapter.details()["resubscribes"] >= 1)
    assert [s["event"] for s in rig.fake.subscriptions] == ["chat.message.sent"]
    assert any(kind == "resubscribed" for kind, _ in rig.logged)
    await close(rig)


async def test_an_existing_subscription_is_not_created_twice(tmp_path):
    fake = FakeKick(KEY)
    fake.subscriptions.append({"id": "old", "event": "chat.message.sent", "version": 1, "method": "webhook"})
    rig = await make_rig(tmp_path, fake=fake)
    await connected(rig)
    await asyncio.sleep(0.2)
    assert [r for r in fake.requests if r == ("POST", "/public/v1/events/subscriptions")] == []
    await close(rig)


async def test_a_taken_webhook_port_is_an_error_and_it_retries(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        rig = await make_rig(tmp_path, kick_webhook_port=taken.getsockname()[1])
        await eventually(lambda: rig.adapter.status().state is ConnectionState.ERROR)
        assert "webhook listener" in rig.adapter.status().detail
        await close(rig)


async def test_if_the_listener_dies_it_is_started_again(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    await rig.adapter._server.stop()  # simulate the listener crashing
    await eventually(lambda: rig.adapter.details()["reconnects"] >= 1)
    await connected(rig)
    await eventually(lambda: rig.adapter._server is not None and rig.adapter._server.running)
    assert (await rig.deliver(chat_payload("after restart"))).status_code == 200
    await close(rig)


async def test_disconnect_frees_the_port_and_connect_works_again(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    port = rig.port
    await rig.adapter.stop()
    assert rig.adapter.status().state is ConnectionState.DISCONNECTED
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # nothing listens any more
    await rig.adapter.start(rig.on_message)
    await connected(rig)
    await close(rig)


async def test_the_no_event_warning_appears_after_two_quiet_minutes(tmp_path):
    rig = await make_rig(tmp_path)
    await connected(rig)
    rig.adapter._connected_at -= 300  # as if it had been connected for five minutes with no event
    assert "NO event received" in rig.adapter._connected_detail() and "tunnel" in rig.adapter._connected_detail()
    await rig.deliver(chat_payload("finally"))
    await eventually(lambda: rig.received)
    assert "receiving events" in rig.adapter._connected_detail()
    await close(rig)


# ---------------------------------------------------------------------------- configuration
def test_kick_settings_defaults_and_validation():
    s = Settings()
    assert s.kick_enabled is False and s.kick_webhook_port == 8787 and s.kick_oauth_port == 8789 and s.kick_message_type == "bot"
    assert s.kick_events == ["chat.message.sent"]
    for bad in [dict(kick_message_type="admin"), dict(kick_events=["made.up"]), dict(kick_events=[]),
                dict(kick_webhook_port=8000), dict(kick_webhook_port=8789), dict(kick_oauth_port=0), dict(kick_webhook_tolerance_s=0),
                dict(kick_resubscribe_interval_s=-1)]:
        with pytest.raises(ValueError):
            Settings(**bad)


async def test_a_kick_viewer_gets_a_reply_posted_to_kick_end_to_end(make_app_client, fake_ollama):
    """Webhook (signed, over real HTTP) -> adapter -> filters -> personality -> (fake) Ollama -> guard -> Kick chat API."""
    fake = FakeKick(KEY)
    client, app = await make_app_client(kick=fake)
    adapter = app.state.container.adapters[Platform.KICK]
    await eventually(lambda: adapter.status().state is ConnectionState.CONNECTED, timeout=10)
    body = body_of(chat_payload("hey stealthbot, what game is this?", username="Alice", user_id=4242))
    async with httpx.AsyncClient() as http:
        out = await http.post(f"http://127.0.0.1:{adapter._server.port}/kick/webhook", content=body,
                              headers=signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZE1", ts(), body))
    assert out.status_code == 200
    await eventually(lambda: fake.sent, timeout=10)
    assert fake.sent[0]["type"] == "bot" and fake.sent[0]["content"]  # a reply was generated, checked and posted as the bot
    rows = (await client.get("/api/messages")).json()
    row = next(r for r in rows if r["message"] == "hey stealthbot, what game is this?")
    assert row["platform"] == "kick" and row["username"] == "Alice" and row["response"]["text"] == fake.sent[0]["content"]
    # the webhook announcing our own message comes back: it must not be answered again
    echo = body_of(chat_payload(fake.sent[0]["content"], username="StealthBot", user_id=31337, message_id=f"msg-{1:08d}-aaaa-bbbb-cccc-dddddddddddd"))
    async with httpx.AsyncClient() as http:
        await http.post(f"http://127.0.0.1:{adapter._server.port}/kick/webhook", content=echo,
                        headers=signed_headers(KEY, "01HZZZZZZZZZZZZZZZZZZZZZE2", ts(), echo))
    await asyncio.sleep(0.5)
    assert len(fake.sent) == 1
    st = (await client.get("/api/status")).json()["adapters"]["kick"]
    assert st["state"] == "connected" and st["sends_ok"] == 1 and st["events"] == 2


async def test_a_kick_outage_cannot_touch_the_rest_of_the_app(make_app_client, fake_ollama):
    fake = FakeKick(KEY)
    fake.fail = {"GET /public/v1/users": 500}  # Kick is down from the start
    client, app = await make_app_client(kick=fake)
    adapter = app.state.container.adapters[Platform.KICK]
    await eventually(lambda: adapter.status().state is ConnectionState.ERROR, timeout=10)
    out = (await client.post("/api/simulate/message", json={"username": "alice", "message": "hello everyone!"})).json()
    assert out["action"] == "replied"  # the simulator (and YouTube) are unaffected
    assert (await client.get("/api/health")).json()["status"] == "ok"


async def test_the_dashboard_can_disconnect_and_reconnect_kick(make_app_client):
    fake = FakeKick(KEY)
    client, app = await make_app_client(kick=fake)
    adapter = app.state.container.adapters[Platform.KICK]
    await eventually(lambda: adapter.status().state is ConnectionState.CONNECTED, timeout=10)
    port = adapter._server.port
    assert (await client.post("/api/platforms/kick/disconnect")).json()["state"] == "disconnected"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # the webhook listener is really gone
    await client.post("/api/platforms/kick/connect")
    await eventually(lambda: adapter.status().state is ConnectionState.CONNECTED, timeout=10)


async def test_the_app_wires_kick_in_only_when_enabled(make_app_client):
    client, app = await make_app_client()
    assert Platform.KICK not in app.state.container.adapters
    client2, app2 = await make_app_client(kick_enabled=True, kick_webhook_port=0)
    adapter = app2.state.container.adapters[Platform.KICK]
    await eventually(lambda: adapter.status().state is ConnectionState.ERROR, timeout=10)  # no stored sign-in in a fresh state dir
    assert "kick_login" in adapter.status().detail
    state = (await client2.get("/api/status")).json()["adapters"]["kick"]
    assert state["state"] == "error" and state["message_type"] == "bot"
