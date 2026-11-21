"""The rest of the dashboard's backend: system stats, logs, session history, AI settings, message limits, platform connect and
disconnect, stop/restart, manual approval, prompt editing."""
import asyncio
import sqlite3
import types

import pytest

from app.chat.models import Category, Platform
from app.config import Settings
from app.run import Lifecycle
from app.services.log_tail import tail_log
from app.services.system_stats import SystemStats, cpu_percent, parse_nvidia_smi
from tests.conftest import FakeClock

SIM = Platform.SIMULATOR


async def say(client, text="hello everyone!", user="alice", **extra):
    return (await client.post("/api/simulate/message", json={"username": user, "message": text, **extra})).json()


# ---------------------------------------------------------------------------- system stats
def test_nvidia_smi_output_is_parsed_and_na_values_become_none():
    gpu = parse_nvidia_smi("NVIDIA GeForce RTX 3050 Laptop GPU, 23, 3119, 4096, 52, 24.50\n")
    assert gpu == {"available": True, "name": "NVIDIA GeForce RTX 3050 Laptop GPU", "utilization_percent": 23.0,
                   "vram_used_mb": 3119.0, "vram_total_mb": 4096.0, "temperature_c": 52.0, "power_w": 24.5}
    assert parse_nvidia_smi("RTX, 5, 100, 4096, [N/A], [N/A]")["power_w"] is None
    assert parse_nvidia_smi("") is None and parse_nvidia_smi("garbage line") is None


def test_cpu_percent_from_two_readings():
    assert cpu_percent((0, 0, 0), (50, 100, 100)) == 75.0  # kernel time includes idle: busy = (200 - 50) / 200
    assert cpu_percent((10, 10, 10), (10, 10, 10)) is None  # no time passed
    assert cpu_percent((0, 0, 0), (500, 100, 100)) == 0.0  # never negative


async def test_system_stats_are_cached_and_survive_missing_hardware():
    calls = {"cpu": 0, "gpu": 0}
    clock = FakeClock(0.0)

    def read_cpu():
        calls["cpu"] += 1
        return (calls["cpu"] * 10, calls["cpu"] * 100, calls["cpu"] * 100)

    def run_gpu():
        calls["gpu"] += 1
        return None  # no nvidia-smi

    stats = SystemStats(clock=clock, read_cpu=read_cpu, read_mem=lambda: {"total_mb": 16000, "used_mb": 8000, "percent": 50.0},
                        read_proc=lambda: {"private_mb": 80.0}, run_gpu=run_gpu, ttl_s=2.0, gpu_ttl_s=5.0)
    first = await stats.snapshot()
    assert first["gpu"] == {"available": False} and first["ram"]["percent"] == 50.0 and first["cpu_percent"] is not None
    await stats.snapshot()
    assert calls["gpu"] == 1  # the second call came from the cache
    clock.advance(3)
    await stats.snapshot()
    assert calls["gpu"] == 1  # the cpu cache expired but the (slower) gpu query did not
    clock.advance(3)
    await stats.snapshot()
    assert calls["gpu"] == 2


async def test_a_failing_reader_gives_empty_values_not_an_error():
    def boom():
        raise OSError("no counters")

    stats = SystemStats(read_cpu=lambda: (0, 0, 0), read_mem=boom, read_proc=boom, run_gpu=lambda: None)
    data = await stats.snapshot()
    assert data["ram"] is None and data["gpu"] == {"available": False}


async def test_system_endpoint_reports_the_session_and_needs_the_key(app_client):
    client, app = app_client
    app.state.container.system_stats = SystemStats(
        read_cpu=lambda: (0, 100, 100), read_mem=lambda: {"total_mb": 16000, "used_mb": 4000, "percent": 25.0},
        read_proc=lambda: {"private_mb": 77.0}, run_gpu=lambda: "RTX, 10, 3000, 4096, 50, 20")
    data = (await client.get("/api/system")).json()
    assert data["ram"]["percent"] == 25.0 and data["gpu"]["vram_total_mb"] == 4096.0 and data["process"]["private_mb"] == 77.0
    assert data["session"]["id"] == 1 and data["session"]["duration_s"] >= 0
    assert (await client.get("/api/system", headers={"X-API-Key": ""})).status_code == 401


# ---------------------------------------------------------------------------- log viewer
def test_log_tail_filters_by_level_joins_tracebacks_and_redacts(tmp_path):
    log = tmp_path / "stealthbot.log"
    log.write_text(
        "2026-11-21 10:00:00,001 INFO    app.main: started\n"
        "2026-11-21 10:00:01,002 WARNING app.ai: slow reply\n"
        "2026-11-21 10:00:02,003 ERROR   app.chat: failed with access_token=SECRETVALUE123\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1\n'
        "2026-11-21 10:00:03,004 DEBUG   app.x: noise\n", encoding="utf-8")
    entries = tail_log(log, min_level="WARNING")
    assert [e["level"] for e in entries] == ["WARNING", "ERROR"]
    assert "SECRETVALUE123" not in entries[1]["message"] and "Traceback" in entries[1]["message"]
    assert len(tail_log(log, min_level="DEBUG")) == 4
    assert [e["message"] for e in tail_log(log, max_lines=1, min_level="DEBUG")] == ["noise"]
    assert tail_log(tmp_path / "missing.log") == []


def test_log_tail_drops_the_half_line_cut_by_the_size_limit(tmp_path):
    log = tmp_path / "stealthbot.log"
    log.write_text("".join(f"2026-11-21 10:00:{i % 60:02d},000 INFO    app.m: line {i}\n" for i in range(400)), encoding="utf-8")
    entries = tail_log(log, max_lines=1000, max_bytes=2000)
    assert entries and all(e["message"].startswith("line ") for e in entries) and entries[-1]["message"] == "line 399"


async def test_logs_endpoint_validates_and_returns_entries(app_client):
    client, _ = app_client
    data = (await client.get("/api/logs", params={"lines": 5, "level": "info"})).json()
    assert data and set(data[0]) == {"time", "level", "logger", "message"} and len(data) <= 5
    assert (await client.get("/api/logs", params={"lines": 0})).status_code == 422
    assert (await client.get("/api/logs", params={"lines": 5000})).status_code == 422
    assert (await client.get("/api/logs", params={"level": "LOUD"})).status_code == 422
    assert (await client.get("/api/logs", headers={"X-API-Key": ""})).status_code == 401


# ---------------------------------------------------------------------------- session history
async def test_sessions_list_shows_live_numbers_for_the_running_session(make_app_client):
    client, app = await make_app_client(stats_flush_interval_s=0.05)
    await say(client)
    await asyncio.sleep(0.3)
    client2, app2 = await make_app_client()  # a restart: session 1 is marked crashed, session 2 runs
    await say(client2, "hello again!", user="bob")
    rows = (await client2.get("/api/sessions")).json()
    assert [r["id"] for r in rows] == [2, 1]
    running, crashed = rows
    assert running["current"] is True and running["status"] == "running" and running["stats"]["messages_received"] >= 1
    assert crashed["status"] == "crashed" and crashed["stats"]["messages_received"] >= 1  # saved by the periodic flush
    assert crashed["duration_seconds"] is not None and "current" not in crashed
    assert (await client2.get("/api/sessions", params={"limit": 0})).status_code == 422


async def test_connection_logs_can_be_filtered_and_validated(app_client):
    client, app = app_client
    c = app.state.container
    from app.chat.normalizer import now_utc

    await c.repo.log_connection("youtube", "connected", "live chat connected", now_utc())
    await c.repo.log_connection("kick", "error", "webhook failed", now_utc())
    both = (await client.get("/api/connection-logs")).json()
    assert [r["platform"] for r in both] == ["kick", "youtube"]  # newest first
    only = (await client.get("/api/connection-logs", params={"platform": "youtube"})).json()
    assert [r["event"] for r in only] == ["connected"]
    assert (await client.get("/api/connection-logs", params={"limit": 9999})).status_code == 422


# ---------------------------------------------------------------------------- AI configuration
async def test_ai_settings_can_be_changed_are_applied_saved_and_restored(make_app_client):
    client, app = await make_app_client()
    shown = (await client.get("/api/ai/settings")).json()
    assert shown["values"]["num_ctx"] == 4096 and shown["limits"]["temperature"] == [0.0, 2.0]
    out = await client.post("/api/ai/settings", json={"num_ctx": 2048, "temperature": 0.5, "generation_timeout_s": 12, "reply_max_words": 25})
    assert out.status_code == 200 and out.json()["saved"] is True
    s = app.state.container.settings
    assert (s.num_ctx, s.temperature, s.generation_timeout_s, s.reply_max_words) == (2048, 0.5, 12.0, 25)
    assert s.generation_deadline_s == 24.0  # the whole call may hold two attempts
    client2, app2 = await make_app_client()  # restart
    assert app2.state.container.settings.num_ctx == 2048 and app2.state.container.settings.temperature == 0.5


async def test_new_ai_settings_reach_the_next_request(app_client, fake_ollama):
    client, _ = app_client
    await client.post("/api/ai/settings", json={"num_ctx": 1024, "num_predict": 33, "temperature": 0.2})
    await say(client, "what game is this?")
    call = fake_ollama.real_calls[-1]
    assert call["options"]["num_ctx"] == 1024 and call["options"]["num_predict"] == 33 and call["options"]["temperature"] == 0.2


@pytest.mark.parametrize("body", [
    {}, {"num_ctx": 100}, {"num_ctx": 999999}, {"num_ctx": 2048.5}, {"temperature": 3}, {"temperature": True},
    {"num_predict": "many"}, {"colour": "red"}, {"reply_max_words": 5}, {"generation_timeout_s": 1}, [],
])
async def test_invalid_ai_settings_are_rejected_without_changing_anything(app_client, body):
    client, app = app_client
    before = (await client.get("/api/ai/settings")).json()["values"]
    assert (await client.post("/api/ai/settings", json=body)).status_code == 422
    assert (await client.get("/api/ai/settings")).json()["values"] == before


async def test_ai_test_button_generates_without_sending_or_storing(app_client, fake_ollama):
    client, app = app_client
    out = await client.post("/api/ai/test")
    assert out.status_code == 200 and out.json()["text"] and out.json()["model"] == "primary-model"
    assert not app.state.container.adapters[SIM].sent and await app.state.container.repo.count_messages() == 0
    fake_ollama.version_ok = False
    assert (await client.post("/api/ai/test")).status_code == 503


# ---------------------------------------------------------------------------- message limits
async def test_rate_limits_apply_to_a_running_limiter_are_saved_and_restored(make_app_client):
    client, app = await make_app_client()
    assert (await say(client, "first hello here", user="u1"))["action"] == "replied"  # creates the limiter
    shown = (await client.get("/api/rate-limits")).json()
    assert {"youtube", "kick", "simulator"} <= set(shown["platforms"]) and shown["limits"]["max_per_window"] == [1, 10000]
    out = await client.post("/api/rate-limits/simulator", json={"max_per_window": 2, "min_gap_s": 0, "window_s": 600})
    assert out.status_code == 200 and out.json()["platforms"]["simulator"]["max_per_window"] == 2
    assert (await say(client, "second hello here", user="u2"))["action"] == "replied"
    third = await say(client, "third hello here", user="u3")  # the first message already counted in the window
    assert third["action"] == "rate_limited" and third["reason"] == "platform_rate_limit"
    client2, app2 = await make_app_client()
    assert app2.state.container.settings.limits_for("simulator").max_per_window == 2


async def test_viewer_cooldown_can_be_edited(app_client):
    client, app = app_client
    await client.post("/api/rate-limits/simulator", json={"user_cooldown_s": 300})
    assert (await say(client, "hello one", user="same"))["action"] == "replied"
    again = await say(client, "hello two", user="same")
    assert again["action"] == "rate_limited" and again["reason"] == "user_cooldown"


@pytest.mark.parametrize("body", [{}, {"max_per_window": 0}, {"max_per_window": 2.5}, {"window_s": 1}, {"min_gap_s": -1},
                                  {"max_chars": 5}, {"nonsense": 1}, {"max_per_window": "ten"}])
async def test_invalid_rate_limits_are_rejected(app_client, body):
    client, app = app_client
    before = (await client.get("/api/rate-limits")).json()
    assert (await client.post("/api/rate-limits/simulator", json=body)).status_code == 422
    assert (await client.get("/api/rate-limits")).json() == before


async def test_rate_limits_for_an_unknown_platform_are_refused(app_client):
    client, _ = app_client
    assert (await client.post("/api/rate-limits/myspace", json={"max_per_window": 5})).status_code == 404


# ---------------------------------------------------------------------------- platforms and stop/restart
async def test_a_platform_can_be_disconnected_and_reconnected(app_client):
    client, app = app_client
    assert (await client.post("/api/platforms/simulator/disconnect")).json() == {"platform": "simulator", "state": "disconnected"}
    status = (await client.get("/api/status")).json()
    assert status["adapters"]["simulator"]["state"] == "disconnected"
    assert (await client.post("/api/platforms/simulator/connect")).json()["state"] == "connected"
    assert (await client.post("/api/platforms/nowhere/connect")).status_code == 404
    assert (await client.post("/api/platforms/nowhere/disconnect")).status_code == 404
    assert (await client.post("/api/platforms/simulator/connect", headers={"X-API-Key": ""})).status_code == 401


async def test_stop_needs_the_launcher_and_answers_before_it_shuts_down(app_client):
    client, app = app_client
    assert (await client.post("/api/control/stop")).status_code == 501  # started without scripts\start.bat
    calls = []
    app.state.lifecycle = types.SimpleNamespace(request_stop=lambda restart: calls.append(restart))
    out = await client.post("/api/control/stop", json={"restart": True})
    assert out.status_code == 202 and out.json() == {"stopping": True, "restart": True}
    await asyncio.sleep(0.2)
    assert calls == []  # the reply has been delivered and the shutdown is still waiting (it needs time to flush the answer)
    await asyncio.sleep(0.6)
    assert calls == [True]
    assert (await client.post("/api/control/stop", json={"restart": "yes"})).status_code == 422
    assert (await client.post("/api/control/stop", headers={"X-API-Key": ""})).status_code == 401


def test_lifecycle_remembers_whether_a_restart_was_asked_for():
    server = types.SimpleNamespace(should_exit=False)
    lifecycle = Lifecycle(server)
    lifecycle.request_stop(restart=True)
    assert server.should_exit is True and lifecycle.restart_requested is True
    stopper = Lifecycle(types.SimpleNamespace(should_exit=False))
    stopper.request_stop(restart=False)
    assert stopper.restart_requested is False


def test_the_launcher_loops_only_when_a_restart_was_requested(monkeypatch):
    import app.run as run

    runs = []

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False
            self.app = config.app

        def run(self):
            runs.append(1)
            self.app.state.lifecycle.request_stop(restart=len(runs) < 3)  # restart twice, then really stop

    monkeypatch.setattr(run.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(run.uvicorn, "Config", lambda app, **kw: types.SimpleNamespace(app=app, **kw))
    monkeypatch.setattr(run, "create_app", lambda settings: types.SimpleNamespace(state=types.SimpleNamespace()))
    monkeypatch.setattr(run, "load_settings", lambda: Settings())
    assert run.main() == 0 and len(runs) == 3


# ---------------------------------------------------------------------------- manual approval
async def test_with_approval_on_a_reply_waits_and_is_sent_only_after_approval(app_client):
    client, app = app_client
    c = app.state.container
    sim = c.adapters[SIM]
    assert (await client.post("/api/control/approval", json={"enabled": True})).json()["approval_required"] is True
    out = await say(client, "hello everyone!")
    assert out["action"] == "held" and out["reply"] and not sim.sent
    queue = (await client.get("/api/approvals")).json()
    assert queue["enabled"] is True and len(queue["items"]) == 1
    item = queue["items"][0]
    assert item["text"] == out["reply"] and item["viewer"] == "alice" and item["viewer_message"] == "hello everyone!"
    assert item["expires_in_s"] <= 120

    done = await client.post(f"/api/approvals/{item['id']}/approve")
    assert done.status_code == 200 and done.json()["action"] == "replied" and list(sim.sent) == [out["reply"]]
    assert (await client.get("/api/approvals")).json()["items"] == []
    assert (await client.post(f"/api/approvals/{item['id']}/approve")).status_code == 404  # cannot be sent twice
    rows = (await client.get("/api/messages")).json()
    assert next(r for r in rows if r["message"] == "hello everyone!")["response"]["delivery_status"] == "sent"


async def test_an_approved_message_can_be_edited_first(app_client):
    client, app = app_client
    await client.post("/api/control/approval", json={"enabled": True})
    await say(client)
    item = (await client.get("/api/approvals")).json()["items"][0]
    done = await client.post(f"/api/approvals/{item['id']}/approve", json={"text": "Welcome in, glad you are here!"})
    assert done.json()["reply"] == "Welcome in, glad you are here!"
    assert list(app.state.container.adapters[SIM].sent) == ["Welcome in, glad you are here!"]
    rows = (await client.get("/api/messages")).json()
    assert next(r for r in rows if r["message"] == "hello everyone!")["response"]["text"] == "Welcome in, glad you are here!"
    assert (await client.post("/api/approvals/1/approve", json={"text": ""})).status_code == 422


async def test_a_rejected_message_is_never_sent(app_client):
    client, app = app_client
    await client.post("/api/control/approval", json={"enabled": True})
    await say(client)
    item = (await client.get("/api/approvals")).json()["items"][0]
    assert (await client.post(f"/api/approvals/{item['id']}/reject")).json() == {"rejected": item["id"]}
    assert not app.state.container.adapters[SIM].sent
    assert (await client.post(f"/api/approvals/{item['id']}/reject")).status_code == 404
    rows = (await client.get("/api/messages")).json()
    assert next(r for r in rows if r["message"] == "hello everyone!")["response"]["delivery_status"] == "rejected"


async def test_nobody_decides_then_the_message_expires(app_client):
    client, app = app_client
    c = app.state.container
    clock = FakeClock(1000.0)
    c.processor.approvals._clock = clock
    await client.post("/api/control/approval", json={"enabled": True})
    await say(client)
    clock.advance(130)  # past the 120 s limit
    assert (await client.get("/api/approvals")).json()["items"] == []
    assert not c.adapters[SIM].sent
    rows = (await client.get("/api/messages")).json()
    assert next(r for r in rows if r["message"] == "hello everyone!")["response"]["delivery_status"] == "expired"


async def test_approval_is_blocked_while_paused_and_the_message_waits(app_client):
    client, app = app_client
    await client.post("/api/control/approval", json={"enabled": True})
    await say(client)
    item = (await client.get("/api/approvals")).json()["items"][0]
    await client.post("/api/control/pause")
    blocked = await client.post(f"/api/approvals/{item['id']}/approve")
    assert blocked.status_code == 409 and blocked.json()["code"] == "outgoing_paused"
    assert len((await client.get("/api/approvals")).json()["items"]) == 1  # still waiting
    await client.post("/api/control/resume")
    assert (await client.post(f"/api/approvals/{item['id']}/approve")).status_code == 200


async def test_approval_respects_the_platform_rate_limit(make_app_client):
    limits = {"simulator": {"min_gap_s": 0, "max_per_window": 1, "window_s": 600, "user_cooldown_s": 0, "max_chars": 500}}
    client, app = await make_app_client(rate_limits=limits)
    await client.post("/api/control/approval", json={"enabled": True})
    assert (await say(client, "hello one", user="u1"))["action"] == "held"  # nothing sent yet: the window is still empty
    await client.post("/api/control/approval", json={"enabled": False})
    assert (await say(client, "hello two", user="u2"))["action"] == "replied"  # this one uses the only message of the window
    held = (await client.get("/api/approvals")).json()["items"][0]
    blocked = await client.post(f"/api/approvals/{held['id']}/approve")  # the limit is checked when it is approved
    assert blocked.status_code == 409 and blocked.json()["code"] == "platform_rate_limit"
    assert len((await client.get("/api/approvals")).json()["items"]) == 1  # and it keeps waiting


async def test_commands_and_switching_approval_off_send_immediately(app_client):
    client, app = app_client
    sim = app.state.container.adapters[SIM]
    await client.post("/api/control/approval", json={"enabled": True})
    assert (await say(client, "!bot", user="c1"))["action"] == "replied"  # the bot answering its own command
    assert len(sim.sent) == 1
    await client.post("/api/control/approval", json={"enabled": False})
    assert (await say(client, "hello there", user="c2"))["action"] == "replied" and len(sim.sent) == 2


async def test_the_approval_queue_is_bounded_and_pushed_out_replies_are_marked(make_app_client):
    client, app = await make_app_client(approval_queue_max=3)
    await client.post("/api/control/approval", json={"enabled": True})
    for i in range(5):
        await say(client, f"hello number {i} everyone", user=f"user{i}")
    items = (await client.get("/api/approvals")).json()["items"]
    assert len(items) == 3
    rows = (await client.get("/api/messages")).json()
    statuses = [r["response"]["delivery_status"] for r in rows if r["response"]]
    assert statuses.count("expired") == 2 and statuses.count("awaiting_approval") == 3


async def test_proactive_messages_are_held_for_approval_too(make_app_client):
    client, app = await make_app_client()
    c = app.state.container
    await client.post("/api/control/approval", json={"enabled": True})
    result = await c.processor.submit_proactive(SIM, Category.PROACTIVE_GENERAL, still_ok=lambda: True)
    assert result.action == "held" and not c.adapters[SIM].sent
    item = (await client.get("/api/approvals")).json()["items"][0]
    assert item["viewer"] is None and item["category"] == "proactive_general"
    assert (await client.post(f"/api/approvals/{item['id']}/approve")).status_code == 200 and len(c.adapters[SIM].sent) == 1


async def test_a_held_proactive_message_restarts_its_timer_instead_of_piling_up(make_app_client):
    client, app = await make_app_client(proactive_platforms=["simulator"], proactive_tick_s=3600)
    c = app.state.container
    await asyncio.sleep(0.05)
    clock = FakeClock(1000.0)
    c.scheduler._clock = clock
    c.scheduler._states.clear()
    await c.scheduler.tick()
    await client.post("/api/control/approval", json={"enabled": True})
    clock.advance(800)
    first = await c.scheduler.tick()
    assert first[0]["action"] == "held" and 400 <= c.scheduler.status()["platforms"]["simulator"]["next_in_s"]["general"] <= 720
    assert c.scheduler.status()["platforms"]["simulator"]["failures"] == 0
    assert len((await client.get("/api/approvals")).json()["items"]) == 1


async def test_approval_routes_need_the_key_and_a_numeric_id(app_client):
    client, _ = app_client
    for method, path in [("get", "/api/approvals"), ("post", "/api/approvals/1/approve"), ("post", "/api/approvals/1/reject"),
                         ("post", "/api/control/approval")]:
        assert (await getattr(client, method)(path, headers={"X-API-Key": ""})).status_code == 401, path
    assert (await client.post("/api/approvals/abc/approve")).status_code == 422
    assert (await client.post("/api/approvals/abc/reject")).status_code == 422


def test_approval_settings_are_validated():
    assert Settings().approval_required is False and Settings().approval_timeout_s == 120.0
    with pytest.raises(ValueError):
        Settings(approval_timeout_s=0)
    with pytest.raises(ValueError):
        Settings(approval_queue_max=0)


# ---------------------------------------------------------------------------- personality prompts
async def test_a_prompt_can_be_edited_is_used_saved_and_can_be_reset(make_app_client):
    client, app = await make_app_client()
    profile = app.state.container.personalities.get("HypeBot")
    original = profile.system_prompt
    new_prompt = "You are HypeBot. Cheer for the streamer in one short, upbeat sentence. Never mention being an AI model."
    out = await client.post("/api/personalities/HypeBot/settings", json={"system_prompt": new_prompt, "description": "Cheers loudly."})
    assert out.status_code == 200 and profile.system_prompt == new_prompt and profile.description == "Cheers loudly."
    shown = next(p for p in (await client.get("/api/personalities")).json() if p["name"] == "HypeBot")
    assert shown["system_prompt"] == new_prompt
    client2, app2 = await make_app_client()  # restart: the edit is remembered
    assert app2.state.container.personalities.get("HypeBot").system_prompt == new_prompt
    reset = await client2.post("/api/personalities/HypeBot/reset")
    assert reset.status_code == 200 and app2.state.container.personalities.get("HypeBot").system_prompt == original
    assert (await client2.post("/api/personalities/Nobody/reset")).status_code == 404


async def test_invalid_prompt_edits_are_rejected(app_client):
    client, app = app_client
    original = app.state.container.personalities.get("HypeBot").system_prompt
    for body in [{"system_prompt": "short"}, {"system_prompt": "x" * 5000}, {"system_prompt": 123}, {"description": "d" * 400}]:
        assert (await client.post("/api/personalities/HypeBot/settings", json=body)).status_code == 422, body
    assert app.state.container.personalities.get("HypeBot").system_prompt == original
    assert (await client.post("/api/personalities/HypeBot/reset", headers={"X-API-Key": ""})).status_code == 401


async def test_the_status_controls_include_the_approval_switch(app_client):
    client, _ = app_client
    assert (await client.get("/api/status")).json()["controls"]["approval_required"] is False
