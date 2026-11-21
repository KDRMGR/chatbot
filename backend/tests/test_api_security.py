import logging

import pytest

from app.config import Settings
from app.logging_setup import redact


async def test_protected_routes_require_the_api_key(app_client):
    client, _ = app_client
    for method, path in [("get", "/api/messages"), ("get", "/api/personalities"), ("get", "/api/status"),
                         ("post", "/api/control/pause"), ("post", "/api/simulate/message")]:
        for headers in ({"X-API-Key": ""}, {"X-API-Key": "wrong"}):
            resp = await getattr(client, method)(path, headers=headers)
            assert resp.status_code == 401, (method, path, headers)


async def test_health_is_public_and_exposes_no_secrets(app_client):
    client, app = app_client
    resp = await client.get("/api/health", headers={"X-API-Key": ""})
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    assert app.state.container.api_token not in resp.text


async def test_foreign_host_headers_are_rejected_to_stop_dns_rebinding(app_client):
    client, _ = app_client
    resp = await client.get("/api/health", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400


async def test_input_validation(app_client):
    client, _ = app_client
    assert (await client.post("/api/simulate/message", json={"username": "", "message": "x"})).status_code == 422
    assert (await client.post("/api/simulate/message", json={"username": "a", "message": "x" * 5000})).status_code == 422
    assert (await client.post("/api/simulate/message", json={"username": "a", "event_type": "bogus"})).status_code == 422
    assert (await client.get("/api/messages", params={"limit": 100000})).status_code == 422


async def test_personality_listing_and_preview_do_not_send_or_start_cooldowns(make_app_client, fake_ollama):
    client, app = await make_app_client(keep_cooldowns=True)  # real cooldowns, or "ready_in_s == 0" proves nothing
    listing = (await client.get("/api/personalities")).json()
    assert [p["name"] for p in listing] == ["ChatBuddy", "HypeBot", "RoastBot", "QuizBot", "StreamBuddy"]
    out = await client.post("/api/personalities/QuizBot/preview", json={"category": "proactive_quiz"})
    assert out.status_code == 200 and out.json()["text"]
    assert not any(app.state.container.adapters[p].sent for p in app.state.container.adapters)
    quiz = next(p for p in (await client.get("/api/personalities")).json() if p["name"] == "QuizBot")
    assert quiz["ready_in_s"] == 0
    assert (await client.post("/api/personalities/Nope/preview", json={})).status_code == 404


async def test_preview_reports_503_when_ai_is_down(app_client, fake_ollama):
    client, _ = app_client
    fake_ollama.version_ok = False
    assert (await client.post("/api/personalities/ChatBuddy/preview", json={})).status_code == 503


def test_secrets_are_redacted_from_logs():
    assert "abc123" not in redact("access_token=abc123&x=1")
    assert "SECRETVALUE1234567" not in redact("Authorization: Bearer SECRETVALUE1234567")
    assert "hunter2" not in redact('{"password": "hunter2"}')
    assert "k3y" not in redact("X-API-Key: k3y-value")
    assert redact("plain message") == "plain message"


async def test_api_token_never_appears_in_logs(make_app_client, caplog):
    caplog.set_level(logging.DEBUG)
    client, app = await make_app_client()
    token = app.state.container.api_token
    await client.get("/api/status")
    assert token not in caplog.text and len(token) >= 32
    assert token not in repr(app.state.container)  # the dataclass field is excluded from repr


def test_ollama_must_stay_on_loopback(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        Settings(state_dir=tmp_path, ollama_host="http://192.168.1.50:11434")
    assert Settings(state_dir=tmp_path, ollama_host="http://localhost:11434")
    assert Settings(state_dir=tmp_path, ollama_host="http://192.168.1.50:11434", allow_remote_ollama=True)
    with pytest.raises(ValueError, match="loopback"):
        Settings(state_dir=tmp_path, host="0.0.0.0")


def test_state_lives_outside_the_project_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    s = Settings()
    assert str(s.database_path).startswith(str(tmp_path)) and "OneDrive" not in str(s.database_path)
