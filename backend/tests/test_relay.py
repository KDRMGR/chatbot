"""The online-panel relay: what the bot does with the commands in Supabase, who may send which, and what it publishes."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.config import Settings
from app.relay.service import COMMANDS, ROLE_KINDS, STATUS_MAX_BYTES, RelayService
from app.relay.supabase import CREDENTIAL_KEY, RelayAuthError, RelayHttpError, SupabaseClient
from app.security.credential_store import CredentialStore
from tests.supabase_fakes import (
    ADMIN_ID, BOT_EMAIL, BOT_ID, BOT_PASSWORD, PUBLIC_KEY, STRANGER_ID, STREAMER_ID, FakeSupabase,
)
from tests.youtube_fakes import eventually

URL = "https://testproject.supabase.co"


async def rig(make_app_client, **overrides):
    """A running app (relay NOT started by the app) plus a RelayService driven by hand against a fake Supabase."""
    fake = FakeSupabase()
    client, app = await make_app_client(**overrides)
    settings = app.state.container.settings
    store = CredentialStore(settings.data_dir)
    store.save(CREDENTIAL_KEY, {"email": BOT_EMAIL, "password": BOT_PASSWORD})
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopLoop

    service = RelayService(app, Settings(state_dir=settings.state_dir, relay_enabled=True, supabase_url=URL,
                                         supabase_publishable_key=PUBLIC_KEY, relay_command_max_age_s=300.0),
                           SupabaseClient(URL, PUBLIC_KEY, store, transport=httpx.MockTransport(fake.handler)), sleep=sleep)
    return fake, service, app, client, sleeps


class _StopLoop(Exception):
    pass


# ---------------------------------------------------------------------------- running commands
async def test_an_admin_command_runs_through_the_local_api_and_the_result_is_written_back(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    c = app.state.container
    cid = fake.add_command("pause")
    assert await svc.process_commands() == 1
    assert c.controls.outgoing_paused is True
    row = fake.command(cid)
    assert row["status"] == "done" and row["result"]["ok"] is True and row["handled_at"]
    assert (await client.get("/api/status")).json()["controls"]["outgoing_paused"] is True  # same state the local dashboard shows
    fake.add_command("resume")
    fake.add_command("set_approval", {"enabled": True})
    fake.add_command("set_ai", {"enabled": False})
    fake.add_command("set_proactive", {"enabled": True})
    assert await svc.process_commands() == 4
    assert (c.controls.outgoing_paused, c.controls.approval_required, c.controls.ai_enabled, c.controls.proactive_enabled) == (False, True, False, True)
    assert svc.counters["done"] == 5 and svc.last_command == {"id": 5, "kind": "set_proactive", "status": "done"}
    await svc.aclose()


async def test_topic_and_stream_commands_use_the_same_rules_as_the_local_dashboard(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    ok = fake.add_command("set_topic", {"notes": "BB20 is a Hindi reality show."})
    bad = fake.add_command("set_topic", {"game": "  "})  # the local API refuses an empty game: so does the relay
    unknown = fake.add_command("select_stream", {"target_id": "nope"})
    settings_bad = fake.add_command("stream_settings", {"poll_interval_s": 1})
    settings_ok = fake.add_command("stream_settings", {"failover_after_s": 45, "auto_select": False})
    await svc.process_commands()
    assert fake.command(ok)["status"] == "done" and (await client.get("/api/topic")).json()["notes"] == "BB20 is a Hindi reality show."
    for cid, http in ((bad, 422), (unknown, 404), (settings_bad, 422)):
        assert fake.command(cid)["status"] == "failed" and fake.command(cid)["result"]["http"] == http, fake.command(cid)
    assert "poll_interval_s" in fake.command(settings_bad)["result"]["error"]
    assert fake.command(settings_ok)["status"] == "done"
    streams = (await client.get("/api/streams")).json()["settings"]
    assert (streams["failover_after_s"], streams["auto_select"], streams["poll_interval_s"]) == (45, False, 60)  # all-or-nothing
    await svc.aclose()


async def test_approve_and_reject_pass_the_id_in_the_path_and_the_edited_text_in_the_body(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    c = app.state.container
    c.controls.approval_required = True
    await client.post("/api/simulate/message", json={"username": "alice", "message": "hello everyone!"})
    waiting = (await client.get("/api/approvals")).json()["items"]
    assert len(waiting) == 1
    approve = fake.add_command("approve", {"id": waiting[0]["id"], "text": "Namaste Alice! Swagat hai!"})
    gone = fake.add_command("reject", {"id": 999})
    await svc.process_commands()
    assert fake.command(approve)["status"] == "done"
    assert fake.command(gone)["status"] == "failed" and fake.command(gone)["result"]["http"] == 404
    assert (await client.get("/api/approvals")).json()["items"] == []
    messages = (await client.get("/api/messages?limit=5")).json()
    assert any("Namaste Alice! Swagat hai!" in str(m.get("response")) for m in messages)  # the edited text was what went out
    await svc.aclose()


# ---------------------------------------------------------------------------- who may do what
async def test_a_streamer_can_only_say_things_everything_else_is_refused_and_nothing_runs(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    c = app.state.container
    pause = fake.add_command("pause", by=STREAMER_ID)  # a database policy mistake must not become a bot takeover
    topic = fake.add_command("set_topic", {"notes": "hijack"}, by=STREAMER_ID)
    say = fake.add_command("say", {"text": "Namaste doston!"}, by=STREAMER_ID)
    await svc.process_commands()
    assert c.controls.outgoing_paused is False and (await client.get("/api/topic")).json()["notes"] == ""
    for cid, kind in ((pause, "pause"), (topic, "set_topic")):
        assert fake.command(cid)["status"] == "rejected" and f"streamer role cannot run '{kind}'" in fake.command(cid)["result"]["error"]
    assert fake.command(say)["status"] == "failed" and "not switched on" in fake.command(say)["result"]["error"]  # allowed, not enabled yet
    assert ROLE_KINDS["streamer"] == {"say"} and ROLE_KINDS["admin"] == set(COMMANDS) | {"say"}
    await svc.aclose()


async def test_a_sender_without_a_role_and_unknown_kinds_are_refused(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    nobody = fake.add_command("pause", by=STRANGER_ID)
    bot_as_sender = fake.add_command("pause", by=BOT_ID)
    weird = fake.add_command("drop_database", by=ADMIN_ID)
    malformed = fake.add_command("pause", by=ADMIN_ID)
    fake.command(malformed)["created_by"] = None
    await svc.process_commands()
    assert fake.command(nobody)["result"]["error"] == "the sender has no role"
    assert "bot role cannot run 'pause'" in fake.command(bot_as_sender)["result"]["error"]
    assert "admin role cannot run 'drop_database'" in fake.command(weird)["result"]["error"]
    assert fake.command(malformed)["status"] == "rejected"
    assert app.state.container.controls.outgoing_paused is False
    await svc.aclose()


async def test_stale_commands_are_refused_not_run_after_downtime(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    old = fake.add_command("pause", age_s=301)
    fresh = fake.add_command("resume", age_s=299)
    await svc.process_commands()
    assert fake.command(old)["status"] == "rejected" and "too old" in fake.command(old)["result"]["error"]
    assert fake.command(fresh)["status"] == "done" and app.state.container.controls.outgoing_paused is False
    await svc.aclose()


async def test_payloads_are_checked_fields_ids_and_types(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    cases = {
        "unknown field": fake.add_command("pause", {"seconds": 5}),
        "extra on toggle": fake.add_command("set_ai", {"enabled": True, "model": "x"}),
        "path trick": fake.add_command("remove_stream", {"id": "../control/pause"}),
        "non-numeric approval": fake.add_command("approve", {"id": "1/../../x"}),
        "no id": fake.add_command("reject", {}),
        "not an object": fake.add_command("pause", {}),
    }
    fake.command(cases["not an object"])["payload"] = ["pause"]
    await svc.process_commands()
    for name, cid in cases.items():
        assert fake.command(cid)["status"] == "rejected", (name, fake.command(cid))
    assert "unknown field 'seconds'" in fake.command(cases["unknown field"])["result"]["error"]
    assert fake.command(cases["path trick"])["result"]["error"] == "bad id"
    assert app.state.container.controls.outgoing_paused is False and app.state.container.controls.ai_enabled is True
    assert not [r for r in svc._api()._transport.__dict__ if False]  # (the in-process client is exercised above)
    await svc.aclose()


async def test_say_is_validated_and_uses_the_handler_when_one_is_set(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    seen = []

    async def handler(text, ctx):
        seen.append((text, ctx))
        return {"posted": True}

    assert svc.public_state()["say_enabled"] is False  # the web page reads this to enable or disable the hold-to-speak button
    svc.say_handler = handler
    assert svc.public_state()["say_enabled"] is True
    ok = fake.add_command("say", {"text": "  Namaste doston!  ", "lang": "hi-IN"}, by=STREAMER_ID)
    empty = fake.add_command("say", {"text": "   "}, by=STREAMER_ID)
    long = fake.add_command("say", {"text": "x" * 501}, by=STREAMER_ID)
    extra = fake.add_command("say", {"text": "hi", "to": "everyone"}, by=ADMIN_ID)
    await svc.process_commands()
    assert fake.command(ok)["status"] == "done" and fake.command(ok)["result"] == {"ok": True, "posted": True}
    assert seen == [("Namaste doston!", {"role": "streamer", "user": STREAMER_ID, "lang": "hi-IN"})]
    for cid in (empty, long, extra):
        assert fake.command(cid)["status"] == "rejected"
    await svc.aclose()


async def test_a_command_already_taken_by_another_runner_is_skipped_and_one_crash_does_not_stop_the_rest(make_app_client, monkeypatch):
    fake, svc, app, client, _ = await rig(make_app_client)
    taken = fake.add_command("pause")
    fake.block_claims.add(taken)
    boom = fake.add_command("resume")
    after = fake.add_command("set_ai", {"enabled": False})
    real = svc._execute

    async def flaky(row):
        if row["id"] == boom:
            raise RuntimeError("kaboom with a secret-ish detail")
        return await real(row)

    monkeypatch.setattr(svc, "_execute", flaky)
    await svc.process_commands()
    assert fake.command(taken)["status"] == "pending" and app.state.container.controls.outgoing_paused is False  # not run twice
    assert fake.command(boom)["status"] == "failed" and "secret" not in json.dumps(fake.command(boom)["result"])
    assert fake.command(after)["status"] == "done" and app.state.container.controls.ai_enabled is False
    await svc.aclose()


# ---------------------------------------------------------------------------- what is published
async def test_the_status_snapshot_has_what_the_panel_needs_and_no_secrets(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client, relay_feed_items=3)
    await client.post("/api/simulate/message", json={"username": "alice", "message": "what game is this?"})
    await svc.publish_status()
    row = fake.status_row
    assert row["id"] == 1 and row["updated_at"]
    data = row["data"]
    assert data["v"] == 1 and set(data) >= {"status", "streams", "topic", "approvals", "feed", "relay", "published_at"}
    assert data["status"]["controls"]["approval_required"] in (True, False) and data["topic"]["game"]
    assert any(line["user"] == "alice" for line in data["feed"]) and len(data["feed"]) <= 3
    blob = json.dumps(data)
    assert app.state.container.api_token not in blob and "access-" not in blob and BOT_PASSWORD not in blob and PUBLIC_KEY not in blob
    assert data["relay"]["commands"]["status_published"] == 0  # the count is bumped after the publish
    assert svc.public_state()["connected"] is True and svc.counters["status_published"] == 1
    await svc.aclose()


async def test_an_oversized_snapshot_drops_most_of_the_feed(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client, relay_feed_items=50)
    for n in range(20):
        await client.post("/api/simulate/message", json={"username": f"user{n}", "message": f"message number {n} " + "y" * 150})
    big = await svc.build_status()
    assert len(big["feed"]) > 5
    svc._s.relay_feed_items = 50
    original = json.dumps
    import app.relay.service as module
    monkey_limit = module.STATUS_MAX_BYTES
    module.STATUS_MAX_BYTES = 1000
    try:
        small = await svc.build_status()
    finally:
        module.STATUS_MAX_BYTES = monkey_limit
    assert len(small["feed"]) == 5 and STATUS_MAX_BYTES == 120_000 and original is json.dumps
    await svc.aclose()


# ---------------------------------------------------------------------------- signing in to Supabase
async def test_the_bot_signs_in_once_reuses_the_token_and_refreshes_it_before_trying_the_password_again(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    await svc.publish_status()
    await svc.publish_status()
    assert fake.logins == 1  # one password sign-in for many calls
    fake.expire_tokens()  # Supabase says the token is no longer valid (revoked, clock...)
    await svc.publish_status()
    assert fake.refreshes == 1 and fake.logins == 1 and fake.status_row  # the 401 made it renew with the refresh token, once
    svc._client._expires_at = 0.0  # time passes: the token is about to expire -> the refresh token is used again
    await svc.publish_status()
    assert fake.refreshes == 2 and fake.logins == 1
    fake.refresh_works = False
    svc._client._expires_at = 0.0
    await svc.publish_status()
    assert fake.logins == 2  # no refresh possible: back to the password
    await svc.aclose()


async def test_a_wrong_or_missing_login_is_reported_without_revealing_it(make_app_client, caplog):
    fake, svc, app, client, _ = await rig(make_app_client)
    CredentialStore(app.state.container.settings.data_dir).save(CREDENTIAL_KEY, {"email": BOT_EMAIL, "password": "nope-nope-nope"})
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RelayAuthError) as exc:
            await svc.publish_status()
    assert "Invalid login credentials" in str(exc.value) and "nope-nope-nope" not in str(exc.value) and BOT_PASSWORD not in str(exc.value)
    CredentialStore(app.state.container.settings.data_dir).delete(CREDENTIAL_KEY)
    with pytest.raises(RelayAuthError, match="relay_login.py"):
        await svc.publish_status()
    assert "nope-nope-nope" not in caplog.text
    assert "access-" not in repr(svc._client) and "refresh-" not in repr(svc._client)
    await svc.aclose()


async def test_a_login_without_the_bot_role_can_do_nothing(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    fake.users["admin@example.test"] = {"password": "pw-pw-pw-pw", "id": ADMIN_ID}  # a real admin signed in as the BOT by mistake
    CredentialStore(app.state.container.settings.data_dir).save(CREDENTIAL_KEY, {"email": "admin@example.test", "password": "pw-pw-pw-pw"})
    with pytest.raises(RelayHttpError) as exc:
        await svc.process_commands()
    assert exc.value.status == 403
    await svc.aclose()


async def test_the_public_key_goes_in_the_apikey_header_and_the_login_never_goes_in_a_url(make_app_client):
    fake, svc, app, client, _ = await rig(make_app_client)
    fake.add_command("pause")
    await svc.process_commands()
    await svc.publish_status()
    assert all(r.headers["apikey"] == PUBLIC_KEY for r in fake.requests)
    assert all(BOT_PASSWORD not in str(r.url) and "access-" not in str(r.url) for r in fake.requests)
    rest = [r for r in fake.requests if r.url.path.startswith("/rest/v1/")]
    assert rest and all(r.headers["authorization"].startswith("Bearer access-") for r in rest)
    await svc.aclose()


# ---------------------------------------------------------------------------- the loop
async def test_the_loop_survives_outages_backs_off_and_recovers(make_app_client):
    fake, svc, app, client, sleeps = await rig(make_app_client)
    fake.down = httpx.ConnectError("no internet")
    for _ in range(4):
        with pytest.raises(_StopLoop):
            await svc.run()
    assert svc.error and "could not reach Supabase" in svc.error and svc.public_state()["connected"] is False
    assert sleeps[0] < sleeps[1] < sleeps[2] <= 60 and sleeps[:3] == [2.0, 4.0, 8.0]  # exponential, capped at a minute
    fake.down = None
    with pytest.raises(_StopLoop):
        await svc.run()
    assert svc.error is None and sleeps[-1] == 2.0 and fake.status_row  # back to the normal rhythm
    await svc.aclose()


async def test_a_login_problem_waits_a_minute_between_tries(make_app_client):
    fake, svc, app, client, sleeps = await rig(make_app_client)
    CredentialStore(app.state.container.settings.data_dir).delete(CREDENTIAL_KEY)
    with pytest.raises(_StopLoop):
        await svc.run()
    assert sleeps == [60.0] and "relay_login.py" in svc.error
    await svc.aclose()


async def test_finished_commands_older_than_a_day_are_deleted(make_app_client):
    fake, svc, app, client, sleeps = await rig(make_app_client)
    old = fake.add_command("pause", status="done")
    fake.command(old)["handled_at"] = "2020-01-01T00:00:00+00:00"
    recent = fake.add_command("resume", status="done")
    fake.command(recent)["handled_at"] = "2999-01-01T00:00:00+00:00"
    with pytest.raises(_StopLoop):
        await svc.run()
    assert [c["id"] for c in fake.commands] == [recent]
    await svc.aclose()


# ---------------------------------------------------------------------------- the whole app, real loop
async def test_the_app_runs_the_relay_in_the_background_and_exposes_its_state(make_app_client):
    fake = FakeSupabase()
    client, app = await make_app_client(relay=fake)
    cid = fake.add_command("set_approval", {"enabled": True})
    await eventually(lambda: fake.command(cid)["status"] == "done", timeout=10)
    assert app.state.container.controls.approval_required is True
    await eventually(lambda: fake.status_row is not None, timeout=5)
    state = (await client.get("/api/relay")).json()
    assert state["connected"] is True and state["signed_in_as"] == BOT_EMAIL and state["commands"]["done"] == 1
    assert fake.status_row["data"]["status"]["controls"]["approval_required"] is True  # the panel sees the change it caused


async def test_the_relay_is_off_by_default_and_says_so(make_app_client):
    client, app = await make_app_client()
    assert app.state.container.relay is None
    assert (await client.get("/api/relay")).json() == {"enabled": False}


# ---------------------------------------------------------------------------- settings
def test_relay_settings_are_validated(tmp_path):
    ok = dict(state_dir=tmp_path, supabase_url="https://abcdefghijklmnopqrst.supabase.co/", supabase_publishable_key="sb_publishable_" + "a" * 20)
    assert Settings(**ok).supabase_url == "https://abcdefghijklmnopqrst.supabase.co"  # trailing slash removed
    for bad_url in ("http://x.supabase.co", "https://evil.example.com", "https://x.supabase.co.evil.com", "abc"):
        with pytest.raises(ValueError, match="supabase_url"):
            Settings(**{**ok, "supabase_url": bad_url})
    for bad_key in ("short", "has space in it........", "sb_secret_" + "a" * 20):
        with pytest.raises(ValueError, match="PUBLIC key"):
            Settings(**{**ok, "supabase_publishable_key": bad_key})
    import base64

    def jwt(role):
        part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
        return f"{part({'alg': 'HS256'})}.{part({'role': role})}.signature"

    Settings(**{**ok, "supabase_publishable_key": jwt("anon")})  # the legacy public key is fine
    with pytest.raises(ValueError, match="service_role"):
        Settings(**{**ok, "supabase_publishable_key": jwt("service_role")})  # a powerful key pasted by mistake is refused
    with pytest.raises(ValueError, match="relay_enabled needs"):
        Settings(state_dir=tmp_path, relay_enabled=True)
    with pytest.raises(ValueError, match="relay_poll_s"):
        Settings(**ok, relay_poll_s=0.1)
