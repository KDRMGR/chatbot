import asyncio
import json
import re
import urllib.parse

import httpx
import pytest

from app.platforms.youtube.oauth import (
    DEFAULT_SCOPES, ClientConfig, LoopbackReceiver, OAuthError, TokenSet, build_auth_url, exchange_code,
    generate_pkce, pkce_challenge, refresh_tokens, run_loopback_login,
)
from tests.youtube_fakes import FakeGoogle, error_body

CLIENT = ClientConfig("client-id.apps.googleusercontent.com", "client-secret-value")


def http_for(google: FakeGoogle) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(google.handler))


def test_pkce_matches_the_rfc7636_test_vector():
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_pkce_pair_is_valid():
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128 and re.fullmatch(r"[A-Za-z0-9._~-]+", verifier)
    assert challenge == pkce_challenge(verifier) and "=" not in challenge
    assert generate_pkce()[0] != verifier


def test_client_json_variants():
    installed = json.dumps({"installed": {"client_id": "a", "client_secret": "b", "redirect_uris": ["http://localhost"]}})
    assert ClientConfig.from_json(installed) == ClientConfig("a", "b")
    assert ClientConfig.from_json(json.dumps({"web": {"client_id": "w"}})) == ClientConfig("w", None)
    assert ClientConfig.from_json(json.dumps({"client_id": "flat"})) == ClientConfig("flat", None)
    for bad in ("not json", "{}", json.dumps({"installed": {}}), json.dumps([1])):
        with pytest.raises((ValueError, AttributeError)):
            ClientConfig.from_json(bad)


def test_auth_url_has_every_required_parameter_and_no_secret():
    url = build_auth_url(CLIENT, "http://127.0.0.1:5555", DEFAULT_SCOPES, "STATE", "CHALLENGE")
    parts = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert q["response_type"] == "code" and q["redirect_uri"] == "http://127.0.0.1:5555"
    assert q["code_challenge"] == "CHALLENGE" and q["code_challenge_method"] == "S256" and q["state"] == "STATE"
    assert q["scope"] == "https://www.googleapis.com/auth/youtube.force-ssl" and q["client_id"] == CLIENT.client_id
    assert "client-secret-value" not in url


async def test_exchange_sends_pkce_and_secret_and_builds_tokens():
    google = FakeGoogle()
    async with http_for(google) as http:
        tokens = await exchange_code(http, CLIENT, "auth-code", "the-verifier", "http://127.0.0.1:5555", clock=lambda: 1000.0)
    form = google.token_requests[0]
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code"
    assert form["code_verifier"] == "the-verifier" and form["redirect_uri"] == "http://127.0.0.1:5555"
    assert form["client_secret"] == "client-secret-value" and form["client_id"] == CLIENT.client_id
    assert tokens.refresh_token == "refresh-new" and tokens.expires_at == 1000.0 + 3599
    assert "access-new" not in repr(tokens) and "refresh-new" not in repr(tokens)


async def test_secret_is_omitted_when_the_client_has_none():
    google = FakeGoogle()
    async with http_for(google) as http:
        await exchange_code(http, ClientConfig("cid"), "c", "v", "http://127.0.0.1:1")
    assert "client_secret" not in google.token_requests[0]


async def test_refresh_keeps_the_existing_refresh_token():
    google = FakeGoogle()
    async with http_for(google) as http:
        fresh = await refresh_tokens(http, CLIENT, "refresh-1", clock=lambda: 50.0)
    assert fresh.refresh_token == "refresh-1" and fresh.access_token == "access-r1" and fresh.expires_at == 3650.0
    assert google.token_requests[0]["grant_type"] == "refresh_token"


async def test_invalid_grant_means_the_user_must_sign_in_again():
    google = FakeGoogle()
    google.refresh_error = (400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    async with http_for(google) as http:
        with pytest.raises(OAuthError) as info:
            await refresh_tokens(http, CLIENT, "dead")
    assert info.value.error == "invalid_grant" and info.value.needs_reauth


async def test_network_failures_are_oauth_errors_not_reauth():
    def boom(request):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        with pytest.raises(OAuthError) as info:
            await refresh_tokens(http, CLIENT, "r")
    assert info.value.error == "network_error" and not info.value.needs_reauth


def test_token_expiry_has_a_safety_skew():
    t = TokenSet("a", "r", expires_at=1000.0)
    assert not t.expired(900.0) and t.expired(950.0) and t.expired(2000.0)


# ---------------------------------------------------------------------------- loopback receiver
async def hit(base: str, query: str, path: str = "/") -> int:
    async with httpx.AsyncClient() as c:
        return (await c.get(f"{base}{path}?{query}")).status_code


async def test_receiver_ignores_forged_requests_and_accepts_the_real_one():
    rx = LoopbackReceiver("good-state")
    base = await rx.start()
    try:
        waiter = asyncio.create_task(rx.wait(5))
        assert await hit(base, "code=evil&state=bad-state") == 400  # wrong state: rejected, sign-in NOT aborted
        assert await hit(base, "", "/favicon.ico") == 404
        assert await hit(base, "code=x", "/other") == 404
        assert not waiter.done()
        assert await hit(base, "code=real-code&state=good-state") == 200
        assert await waiter == "real-code"
    finally:
        await rx.close()


async def test_receiver_reports_a_declined_sign_in():
    rx = LoopbackReceiver("s")
    base = await rx.start()
    try:
        waiter = asyncio.create_task(rx.wait(5))
        await hit(base, "error=access_denied&state=s")
        with pytest.raises(OAuthError) as info:
            await waiter
        assert info.value.error == "access_denied"
    finally:
        await rx.close()


async def test_receiver_times_out():
    rx = LoopbackReceiver("s")
    await rx.start()
    try:
        with pytest.raises(OAuthError) as info:
            await rx.wait(0.1)
        assert info.value.error == "timeout"
    finally:
        await rx.close()


async def test_receiver_never_echoes_request_data():
    rx = LoopbackReceiver("s")
    base = await rx.start()
    try:
        async with httpx.AsyncClient() as c:
            resp = await c.get(f"{base}/?code=<script>alert(1)</script>&state=nope")
        assert "<script>" not in resp.text and "alert" not in resp.text
    finally:
        await rx.close()


async def test_full_loopback_login_verifies_state_and_pkce():
    google = FakeGoogle()
    seen: dict[str, str] = {}

    def fake_browser(url: str) -> bool:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.update(q)

        async def redirect_back():  # what Google does after the user approves
            await asyncio.sleep(0.05)
            await hit(q["redirect_uri"], f"code=the-code&state={q['state']}")

        asyncio.get_running_loop().create_task(redirect_back())
        return True

    async with http_for(google) as http:
        tokens = await run_loopback_login(http, CLIENT, opener=fake_browser, timeout=5, announce=lambda s: None)
    form = google.token_requests[0]
    assert form["code"] == "the-code" and form["redirect_uri"] == seen["redirect_uri"]
    assert pkce_challenge(form["code_verifier"]) == seen["code_challenge"]  # the verifier matches the challenge sent
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+", seen["redirect_uri"])
    assert tokens.refresh_token == "refresh-new"
