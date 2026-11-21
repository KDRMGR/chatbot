"""Runtime model switching, personality switches, one-time dashboard sign-in and the dashboard files."""
import re
from pathlib import Path

import httpx
import pytest

from app.ai.model_manager import ModelManager, ModelSwitchRejected
from app.ai.ollama_client import OllamaClient
from app.api.dashboard import STATIC_DIR
from app.api.security import TicketStore
from tests.conftest import FakeClock

MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mm(settings, fake_ollama, clock) -> ModelManager:
    client = OllamaClient(settings.ollama_host, transport=httpx.MockTransport(fake_ollama.handler))
    return ModelManager(client, settings, clock=clock)


# ---------------------------------------------------------------------------- ModelManager
async def test_switch_primary_tests_the_model_then_uses_it(mm, fake_ollama):
    fake_ollama.installed.append("new-model")
    result = await mm.switch_primary("new-model")
    assert result["primary"] == "new-model" and result["previous"] == "primary-model" and result["sample"]
    # the candidate got a real test reply with the same options real replies use (no reload for the first viewer)
    test_call = fake_ollama.real_calls[0]
    assert test_call["model"] == "new-model" and test_call["options"]["num_gpu"] == 99
    assert test_call["options"]["num_ctx"] == 4096 and test_call["think"] is False
    assert fake_ollama.unloaded == ["primary-model"]  # the old model leaves the GPU

    fake_ollama.chat_calls.clear()
    assert (await mm.generate(MSGS)).model == "new-model"
    assert mm.status()["primary_model"] == "new-model" and mm.status()["switching_to"] is None


async def test_failed_test_keeps_the_old_model_active(mm, fake_ollama):
    fake_ollama.installed.append("bad-model")
    fake_ollama.fail_models["bad-model"] = "500"
    with pytest.raises(ModelSwitchRejected) as err:
        await mm.switch_primary("bad-model")
    assert err.value.code == "failed" and "primary-model" in err.value.reason
    assert mm.primary == "primary-model" and not fake_ollama.unloaded
    assert (await mm.generate(MSGS)).model == "primary-model"  # chat is unaffected
    assert mm.status()["switching_to"] is None


async def test_empty_test_reply_is_a_failed_switch(mm, fake_ollama):
    fake_ollama.installed.append("mute-model")
    fake_ollama.reply = "   "
    with pytest.raises(ModelSwitchRejected) as err:
        await mm.switch_primary("mute-model")
    assert err.value.code == "failed" and mm.primary == "primary-model"


async def test_switch_rejects_missing_unchanged_invalid_and_ollama_down(mm, fake_ollama):
    for name, code in [("ghost-model", "not_installed"), ("primary-model", "unchanged"), ("   ", "invalid"),
                       ("x" * 300, "invalid")]:
        with pytest.raises(ModelSwitchRejected) as err:
            await mm.switch_primary(name)
        assert err.value.code == code, name
    assert "ollama pull ghost-model" in (await _reason(mm, "ghost-model"))
    fake_ollama.version_ok = False
    with pytest.raises(ModelSwitchRejected) as err:
        await mm.switch_primary("fallback-model")
    assert err.value.code == "ollama_down" and mm.primary == "primary-model"


async def _reason(mm, name):
    with pytest.raises(ModelSwitchRejected) as err:
        await mm.switch_primary(name)
    return err.value.reason


async def test_switching_to_the_backup_promotes_the_old_primary_to_backup(mm):
    result = await mm.switch_primary("fallback-model")
    assert mm.primary == "fallback-model" and mm.fallback == "primary-model"
    assert result["fallback_changed"] is True


async def test_fallback_can_be_chosen_removed_and_must_differ_from_primary(mm, fake_ollama):
    fake_ollama.installed.append("third-model")
    assert (await mm.set_fallback("third-model"))["fallback"] == "third-model"
    assert (await mm.set_fallback(None))["fallback"] is None and mm.fallback is None
    for name, code in [("primary-model", "invalid"), ("ghost-model", "not_installed")]:
        with pytest.raises(ModelSwitchRejected) as err:
            await mm.set_fallback(name)
        assert err.value.code == code
    assert not fake_ollama.real_calls  # choosing a backup never loads it (that would evict the primary on 4 GB)


async def test_generate_waiting_during_a_switch_uses_the_new_primary(mm, fake_ollama):
    """A reply that was queued behind the swap must not be written by the model that was just replaced."""
    import asyncio

    await mm._sem.acquire()  # the AI's turn is taken (as it is during a switch or another reply)
    waiting = asyncio.create_task(mm.generate(MSGS))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    mm._primary, mm._fallback = "fallback-model", None  # exactly what a finished switch leaves behind
    mm._sem.release()
    assert (await waiting).model == "fallback-model"
    assert {c["model"] for c in fake_ollama.real_calls} == {"fallback-model"}


async def test_restore_ignores_a_saved_model_that_is_no_longer_installed(mm):
    await mm.restore("deleted-model", None, have_fallback_choice=False)
    assert mm.primary == "primary-model" and mm.fallback == "fallback-model"


async def test_restore_applies_saved_choices_and_never_leaves_primary_equal_to_backup(mm):
    await mm.restore("fallback-model", None, have_fallback_choice=False)
    assert mm.primary == "fallback-model" and mm.fallback is None  # the configured backup became the primary
    await mm.restore(None, None, have_fallback_choice=True)
    assert mm.fallback is None


# ---------------------------------------------------------------------------- API: models and personalities
async def test_models_endpoint_lists_installed_and_current(app_client, fake_ollama):
    client, _ = app_client
    fake_ollama.installed.append("new-model")
    data = (await client.get("/api/models")).json()
    assert data["installed"] == ["primary-model", "fallback-model", "new-model"]
    assert data["primary"] == "primary-model" and data["fallback"] == "fallback-model" and data["ollama_up"] is True
    fake_ollama.version_ok = False
    assert (await client.get("/api/models")).json()["ollama_up"] is False


async def test_switch_endpoint_success_is_saved_and_failures_map_to_http_codes(app_client, fake_ollama):
    client, app = app_client
    fake_ollama.installed.extend(["new-model", "bad-model"])
    fake_ollama.fail_models["bad-model"] = "500"
    repo = app.state.container.repo

    ok = await client.post("/api/control/model", json={"model": "new-model"})
    assert ok.status_code == 200 and ok.json()["changed"] is True and ok.json()["primary"] == "new-model"
    assert await repo.get_setting("ai.primary_model") == "new-model"
    assert (await client.get("/api/status")).json()["ai"]["primary_model"] == "new-model"

    assert (await client.post("/api/control/model", json={"model": "new-model"})).json()["changed"] is False
    assert (await client.post("/api/control/model", json={"model": "ghost"})).status_code == 404
    assert (await client.post("/api/control/model", json={"model": "bad-model"})).status_code == 409
    assert await repo.get_setting("ai.primary_model") == "new-model"  # a failed switch is never remembered
    assert (await client.post("/api/control/model", json={})).status_code == 422
    assert (await client.post("/api/control/model", json={"model": "x", "role": "boss"})).status_code == 422
    fake_ollama.version_ok = False
    assert (await client.post("/api/control/model", json={"model": "fallback-model"})).status_code == 503


async def test_fallback_endpoint_and_none(app_client, fake_ollama):
    client, app = app_client
    assert (await client.post("/api/control/model", json={"role": "fallback", "model": None})).json()["fallback"] is None
    assert await app.state.container.repo.get_setting("ai.fallback_model") == ""  # "" = deliberately no backup
    assert (await client.get("/api/models")).json()["fallback"] is None


async def test_saved_model_choice_survives_a_restart(make_app_client, fake_ollama):
    client, app = await make_app_client()
    fake_ollama.installed.append("new-model")
    assert (await client.post("/api/control/model", json={"model": "new-model"})).status_code == 200
    client2, app2 = await make_app_client()  # a second start on the same state directory
    assert app2.state.container.model_manager.primary == "new-model"
    assert (await client2.get("/api/status")).json()["ai"]["primary_model"] == "new-model"


async def test_personality_switch_is_applied_saved_and_validated(make_app_client):
    client, app = await make_app_client()
    c = app.state.container
    off = await client.post("/api/personalities/RoastBot/enabled", json={"enabled": False})
    assert off.status_code == 200 and off.json() == {"name": "RoastBot", "enabled": False}
    assert c.personalities.get("RoastBot").enabled is False
    assert next(p for p in await c.repo.list_personalities() if p["name"] == "RoastBot")["enabled"] is False
    client2, app2 = await make_app_client()  # restart: the choice is remembered
    assert app2.state.container.personalities.get("RoastBot").enabled is False
    assert (await client.post("/api/personalities/RoastBot/enabled", json={"enabled": True})).status_code == 200
    assert (await client.post("/api/personalities/Nobody/enabled", json={"enabled": True})).status_code == 404
    assert (await client.post("/api/personalities/RoastBot/enabled", json={"enabled": "yes"})).status_code == 422


async def test_personality_numbers_can_be_edited_are_saved_and_validated(make_app_client):
    client, app = await make_app_client(keep_cooldowns=True)
    ok = await client.post("/api/personalities/StreamBuddy/settings", json={"cooldown_seconds": 15, "priority": 5, "frequency": 0.7})
    assert ok.status_code == 200 and ok.json() == {"name": "StreamBuddy", "cooldown_seconds": 15, "frequency": 0.7, "priority": 5}
    profile = app.state.container.personalities.get("StreamBuddy")
    assert (profile.cooldown_seconds, profile.frequency, profile.priority) == (15, 0.7, 5)
    partial = await client.post("/api/personalities/StreamBuddy/settings", json={"cooldown_seconds": 40})
    assert partial.json()["priority"] == 5  # fields that were not sent keep their value
    client2, app2 = await make_app_client(keep_cooldowns=True)  # restart: remembered
    assert app2.state.container.personalities.get("StreamBuddy").cooldown_seconds == 40
    for body in [{}, {"cooldown_seconds": -1}, {"cooldown_seconds": 1.5}, {"cooldown_seconds": 10**6}, {"frequency": 11},
                 {"priority": True}, {"colour": "red"}, {"cooldown_seconds": "soon"}, []]:
        assert (await client.post("/api/personalities/StreamBuddy/settings", json=body)).status_code == 422, body
    assert app.state.container.personalities.get("StreamBuddy").cooldown_seconds == 40  # nothing invalid was applied
    assert (await client.post("/api/personalities/Nobody/settings", json={"priority": 1})).status_code == 404
    assert (await client.post("/api/personalities/StreamBuddy/settings", json={"priority": 1}, headers={"X-API-Key": ""})).status_code == 401


async def test_new_control_routes_require_the_api_key(app_client):
    client, _ = app_client
    for method, path in [("get", "/api/models"), ("post", "/api/control/model"),
                         ("post", "/api/personalities/ChatBuddy/enabled"), ("post", "/api/auth/ticket")]:
        resp = await getattr(client, method)(path, headers={"X-API-Key": ""})
        assert resp.status_code == 401, path


# ---------------------------------------------------------------------------- one-time sign-in tickets
def test_ticket_is_single_use_and_expires():
    clock = FakeClock()
    store = TicketStore(ttl_s=60, clock=clock)
    ticket = store.issue()
    assert store.redeem(ticket) is True and store.redeem(ticket) is False  # single use
    old = store.issue()
    clock.advance(61)
    assert store.redeem(old) is False  # expired
    assert store.redeem("") is False and store.redeem("garbage") is False


def test_ticket_store_stays_bounded():
    store = TicketStore(max_outstanding=3)
    tickets = [store.issue() for _ in range(10)]
    assert store.redeem(tickets[0]) is False  # the oldest were dropped
    assert store.redeem(tickets[-1]) is True


async def test_ticket_flow_exchanges_a_ticket_for_the_key_once(app_client):
    client, app = app_client
    issued = await client.post("/api/auth/ticket")
    ticket = issued.json()["ticket"]
    assert app.state.container.api_token not in issued.text  # a ticket is not the key
    anonymous = {"X-API-Key": ""}
    redeemed = await client.post("/api/auth/redeem", json={"ticket": ticket}, headers=anonymous)
    assert redeemed.status_code == 200 and redeemed.json()["api_key"] == app.state.container.api_token
    assert redeemed.headers["cache-control"] == "no-store"
    assert (await client.post("/api/auth/redeem", json={"ticket": ticket}, headers=anonymous)).status_code == 401
    assert (await client.post("/api/auth/redeem", json={"ticket": "nope"}, headers=anonymous)).status_code == 401
    assert (await client.post("/api/auth/redeem", json={}, headers=anonymous)).status_code == 422


# ---------------------------------------------------------------------------- dashboard files
async def test_dashboard_is_served_with_strict_headers_and_no_secrets(app_client):
    client, app = app_client
    anonymous = {"X-API-Key": ""}
    for path, kind in [("/dashboard", "text/html"), ("/dashboard/app.js", "javascript"), ("/dashboard/panels.js", "javascript"),
                       ("/dashboard/style.css", "text/css")]:
        resp = await client.get(path, headers=anonymous)
        assert resp.status_code == 200 and kind in resp.headers["content-type"], path
        assert "script-src 'self'" in resp.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
        assert resp.headers["x-content-type-options"] == "nosniff" and resp.headers["cache-control"] == "no-store"
        assert app.state.container.api_token not in resp.text
    root = await client.get("/", headers=anonymous)
    assert root.status_code == 307 and root.headers["location"] == "/dashboard"


async def test_only_whitelisted_files_are_served(app_client):
    client, _ = app_client
    for path in ["/dashboard/../config.py", "/dashboard/%2e%2e/config.py", "/dashboard/index.html",
                 "/dashboard/secret.txt", "/dashboard/app.js.map", "/static/app.js"]:
        assert (await client.get(path)).status_code == 404, path


async def test_dashboard_refuses_foreign_host_headers(app_client):
    client, _ = app_client
    assert (await client.get("/dashboard", headers={"Host": "evil.example.com"})).status_code == 400


def test_dashboard_code_cannot_inject_chat_text_as_markup():
    """Chat text comes from strangers. The page may only use textContent-style rendering."""
    scripts = sorted(STATIC_DIR.glob("*.js"))
    assert {p.name for p in scripts} == {"app.js", "panels.js"}  # a new script must be added to the whitelist and covered here
    for script in scripts:
        code = re.sub(r"/\*.*?\*/", "", script.read_text(encoding="utf-8"), flags=re.S)  # headers name the banned calls on purpose
        for banned in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\"",
                       "setInterval(\""]:
            assert banned not in code, (script.name, banned)


def test_dashboard_html_has_no_inline_script_or_style():
    """The Content-Security-Policy would block them anyway; keeping them out is what makes the policy strict."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<style" not in html.lower() and not re.search(r"\sstyle\s*=", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)  # onclick= and friends
    assert not re.search(r"<script(?![^>]*\ssrc=)", html, re.I)
    assert "http://" not in html and "https://" not in html  # nothing is loaded from another site


def test_every_element_id_used_by_the_script_exists_in_the_page():
    js = "".join(p.read_text(encoding="utf-8") for p in sorted(STATIC_DIR.glob("*.js")))
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    page_ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r'\$\("([^"]+)"\)', js)) | set(re.findall(r'(?:setText|say)\("([^"]+)"', js))
    # ids built from a template, e.g. $(`tab-${t}`), cannot be listed literally: check the ones they produce
    used |= {f"tab-{t}" for t in ("live", "personalities", "ai", "scheduler", "history")}
    used |= {f"panel-{t}" for t in ("live", "personalities", "ai", "scheduler", "history")}
    used |= {f"act-{n}-{part}" for n in ("general", "gaming_question", "trivia") for part in ("on", "min", "max")}
    assert used - page_ids == set()
