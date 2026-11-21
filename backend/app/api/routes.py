from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..ai.model_manager import AIUnavailable, ModelSwitchRejected
from ..ai.response_generator import GenerationFailed
from ..chat.models import ChatMessage, Platform
from ..chat.normalizer import now_utc
from ..chat.processor import ApprovalBlocked
from ..personalities.defaults import default_personality_rows
from ..scheduler.engagement import SchedulerSettingsError
from ..services.log_tail import LEVELS, tail_log
from ..knowledge.topic import TopicError
from ..streams.model import StreamsError
from ..services.runtime_settings import (
    AI_FIELDS, RATE_FIELDS, SettingsError, ai_values, apply_ai, apply_rate_limits, parse_numbers, rate_values,
)
from .schemas import (
    AIToggle, ApprovalDecision, ModelChange, PersonalitySettings, PreviewRequest, SimulateRequest, StopRequest,
    TicketRedeem, ValidationFailed,
)
from .security import protected

logger = logging.getLogger(__name__)


def _c(request: Request):  # noqa: ANN202
    return request.app.state.container


async def _json_body(request: Request, *, optional: bool = False) -> Any:
    raw = await request.body()
    if optional and not raw:
        return {}
    try:
        return await request.json()
    except ValueError as exc:
        raise ValidationFailed([{"loc": ["body"], "msg": "invalid JSON"}]) from exc


async def validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ValidationFailed)
    return JSONResponse({"detail": exc.errors}, status_code=422)


def _controls(c: Any) -> dict[str, bool]:
    return {"outgoing_paused": c.controls.outgoing_paused, "ai_enabled": c.controls.ai_enabled,
            "proactive_enabled": c.controls.proactive_enabled, "approval_required": c.controls.approval_required}


# ---------------------------------------------------------------------------- public (no secrets exposed)
async def health(request: Request) -> JSONResponse:
    c = _c(request)
    ai = c.model_manager.status()
    return JSONResponse({
        "status": "ok", "ollama_up": ai.get("ollama_up"), "active_model": ai.get("active_model"), **_controls(c),
    })


# ---------------------------------------------------------------------------- protected
@protected
async def simulate_message(request: Request) -> JSONResponse:
    c = _c(request)
    body = SimulateRequest.parse(await _json_body(request))
    try:
        msg = ChatMessage(
            platform=Platform.SIMULATOR, viewer_id=body.viewer_id or f"sim-{body.username.lower()}",
            username=body.username, message=body.message, message_id=body.message_id,
            event_type=body.event_type, amount=body.amount,
        )
    except ValueError as exc:
        raise ValidationFailed([{"loc": ["body"], "msg": str(exc)}]) from exc
    try:
        result = await c.processor.submit(msg)
    except asyncio.TimeoutError:
        return JSONResponse({"detail": "Timed out waiting for the message to be processed"}, status_code=504)
    return JSONResponse(result.to_dict())


@protected
async def messages(request: Request) -> JSONResponse:
    raw = request.query_params.get("limit", "50")
    if not raw.isdigit() or not 1 <= int(raw) <= 500:
        raise ValidationFailed([{"loc": ["limit"], "msg": "must be an integer between 1 and 500"}])
    return JSONResponse(await _c(request).repo.recent_activity(int(raw)))


@protected
async def personalities(request: Request) -> JSONResponse:
    mgr = _c(request).personalities
    return JSONResponse([
        {
            "name": p.name, "description": p.description, "enabled": p.enabled, "categories": p.categories,
            "cooldown_seconds": p.cooldown_seconds, "frequency": p.frequency, "priority": p.priority,
            "ready_in_s": round(mgr.seconds_until_ready(p.name), 1), "system_prompt": p.system_prompt,
        }
        for p in mgr.all()
    ])


@protected
async def preview(request: Request) -> JSONResponse:
    """Generate a sample reply WITHOUT sending, storing or starting any cooldown."""
    c = _c(request)
    name = request.path_params["name"]
    body = PreviewRequest.parse(await _json_body(request, optional=True))
    profile = c.personalities.get(name)
    if profile is None:
        return JSONResponse({"detail": f"Unknown personality {name!r}"}, status_code=404)
    try:
        reply = await c.generator.generate(
            profile, body.category, platform=Platform.SIMULATOR.value, username=body.username,
            message=body.message or None,
        )
    except GenerationFailed as exc:
        return JSONResponse({"detail": f"No reply available: {exc.reason}"}, status_code=503)
    return JSONResponse({"personality": name, "text": reply.text, "model": reply.model, "latency_ms": reply.latency_ms})


@protected
async def status(request: Request) -> JSONResponse:
    c = _c(request)
    return JSONResponse({
        "session_id": c.session_manager.session_id,
        "controls": _controls(c),
        "ai": c.model_manager.status(),
        "metrics": c.metrics.snapshot(),
        "queue_size": c.processor.incoming.qsize(),
        "queue_dropped": c.processor.incoming.dropped,
        "adapters": {
            p.value: {
                "state": a.status().state.value, "detail": a.status().detail,
                **(a.details() if hasattr(a, "details") else {}),
            }
            for p, a in c.adapters.items()
        },
    })


@protected
async def pause(request: Request) -> JSONResponse:
    """Emergency stop for ALL outgoing messages. Works instantly and does not involve the AI."""
    c = _c(request)
    c.controls.outgoing_paused = True
    return JSONResponse(_controls(c))


@protected
async def resume(request: Request) -> JSONResponse:
    c = _c(request)
    c.controls.outgoing_paused = False
    return JSONResponse(_controls(c))


@protected
async def toggle_ai(request: Request) -> JSONResponse:
    c = _c(request)
    c.controls.ai_enabled = AIToggle.parse(await _json_body(request)).enabled
    return JSONResponse(_controls(c))


@protected
async def toggle_proactive(request: Request) -> JSONResponse:
    """The streamer can stop the bot from ever starting a conversation (it still answers viewers)."""
    c = _c(request)
    c.controls.proactive_enabled = AIToggle.parse(await _json_body(request)).enabled
    return JSONResponse(_controls(c))


@protected
async def scheduler_status(request: Request) -> JSONResponse:
    return JSONResponse(_c(request).scheduler.status())


@protected
async def scheduler_settings(request: Request) -> JSONResponse:
    """Change quiet-chat time, the minimum gap and each activity's interval range. All or nothing."""
    c = _c(request)
    try:
        return JSONResponse(await c.scheduler.update_settings(await _json_body(request)))
    except SchedulerSettingsError as exc:
        raise ValidationFailed(exc.errors) from exc


@protected
async def models(request: Request) -> JSONResponse:
    """Installed Ollama models plus which ones the bot uses (refreshes the list from Ollama right now)."""
    mm = _c(request).model_manager
    health_now = await mm.health_check()
    st = mm.status()
    return JSONResponse({
        "ollama_up": health_now.get("ollama_up"), "error": health_now.get("error"),
        "installed": health_now.get("installed", []), "loaded": health_now.get("loaded", []),
        "primary": st["primary_model"], "fallback": st["fallback_model"], "active": st["active_model"],
        "switching_to": st["switching_to"],
    })


_SWITCH_STATUS = {"not_installed": 404, "invalid": 422, "ollama_down": 503, "busy": 409, "failed": 409}


@protected
async def change_model(request: Request) -> JSONResponse:
    """Switch the primary (tested first; the old model stays if the test fails) or choose the fallback model."""
    c = _c(request)
    body = ModelChange.parse(await _json_body(request))
    try:
        if body.role == "primary":
            result = await c.model_manager.switch_primary(body.model or "")
        else:
            result = await c.model_manager.set_fallback(body.model)
    except ModelSwitchRejected as exc:
        if exc.code == "unchanged":
            return JSONResponse({"changed": False, "detail": exc.reason, "primary": c.model_manager.primary})
        return JSONResponse({"detail": exc.reason, "code": exc.code}, status_code=_SWITCH_STATUS.get(exc.code, 409))
    # Remember the choice so it survives a restart (it wins over STEALTHBOT_PRIMARY_MODEL until changed again).
    key, value = ("ai.primary_model", result["primary"]) if body.role == "primary" else (
        "ai.fallback_model", result["fallback"] or "")
    try:
        await c.repo.set_setting(key, value, now_utc())
        if result.get("fallback_changed"):  # the old backup became the primary, so the backup changed too
            await c.repo.set_setting("ai.fallback_model", result["fallback"] or "", now_utc())
    except Exception:  # noqa: BLE001 - the switch itself worked; only remembering it failed
        logger.exception("could not save the model choice")
        result["saved"] = False
    return JSONResponse({"changed": True, **result})


@protected
async def set_personality_enabled(request: Request) -> JSONResponse:
    c = _c(request)
    name = request.path_params["name"]
    profile = c.personalities.get(name)
    if profile is None:
        return JSONResponse({"detail": f"Unknown personality {name!r}"}, status_code=404)
    enabled = AIToggle.parse(await _json_body(request)).enabled
    await c.repo.set_personality_enabled(name, enabled, now_utc())
    profile.enabled = enabled
    return JSONResponse({"name": name, "enabled": enabled})


@protected
async def personality_settings(request: Request) -> JSONResponse:
    """Change a personality's cooldown, how often it is picked (frequency) or its priority. Saved across restarts."""
    c = _c(request)
    name = request.path_params["name"]
    profile = c.personalities.get(name)
    if profile is None:
        return JSONResponse({"detail": f"Unknown personality {name!r}"}, status_code=404)
    fields = PersonalitySettings.parse(await _json_body(request)).fields
    await c.repo.update_personality(name, fields, now_utc())
    for key, value in fields.items():
        setattr(profile, key, value)
    return JSONResponse({"name": name, "cooldown_seconds": profile.cooldown_seconds, "frequency": profile.frequency,
                         "priority": profile.priority})


@protected
async def reset_personality(request: Request) -> JSONResponse:
    """Put a personality's prompt and description back to the shipped default (the safety rules are in code, not here)."""
    c = _c(request)
    name = request.path_params["name"]
    profile = c.personalities.get(name)
    default = next((r for r in default_personality_rows() if r["name"] == name), None)
    if profile is None or default is None:
        return JSONResponse({"detail": f"No default exists for {name!r}"}, status_code=404)
    fields = {"system_prompt": default["system_prompt"], "description": default["description"]}
    await c.repo.update_personality(name, fields, now_utc())
    for key, value in fields.items():
        setattr(profile, key, value)
    return JSONResponse({"name": name, **fields})


# ---------------------------------------------------------------------------- overview, history, logs
@protected
async def system_stats(request: Request) -> JSONResponse:
    c = _c(request)
    data = dict(await c.system_stats.snapshot())
    started = c.session_manager.started_at
    data["session"] = {"id": c.session_manager.session_id, "started_at": started.isoformat(),
                       "duration_s": int((now_utc() - started).total_seconds())}
    return JSONResponse(data)


@protected
async def logs(request: Request) -> JSONResponse:
    raw = request.query_params.get("lines", "200")
    level = request.query_params.get("level", "INFO").upper()
    if not raw.isdigit() or not 1 <= int(raw) <= 1000:
        raise ValidationFailed([{"loc": ["lines"], "msg": "must be an integer between 1 and 1000"}])
    if level not in LEVELS:
        raise ValidationFailed([{"loc": ["level"], "msg": f"must be one of {sorted(LEVELS)}"}])
    path = _c(request).settings.log_dir / "stealthbot.log"
    return JSONResponse(await asyncio.to_thread(tail_log, path, max_lines=int(raw), min_level=level))


@protected
async def sessions(request: Request) -> JSONResponse:
    c = _c(request)
    raw = request.query_params.get("limit", "30")
    if not raw.isdigit() or not 1 <= int(raw) <= 200:
        raise ValidationFailed([{"loc": ["limit"], "msg": "must be an integer between 1 and 200"}])
    rows = await c.repo.list_sessions(int(raw))
    now = now_utc()
    for row in rows:
        if row["id"] == c.session_manager.session_id:  # the running session: live numbers, not the last saved snapshot
            snapshot = c.metrics.snapshot()
            row["stats"] = {**row["stats"], **{k: float(v) for k, v in snapshot["counters"].items()},
                            "uptime_s": float(snapshot["uptime_s"])}
            for key, name in (("median", "ai_latency_ms_median"), ("p95", "ai_latency_ms_p95")):
                if snapshot["ai_latency_ms"][key] is not None:
                    row["stats"][name] = float(snapshot["ai_latency_ms"][key])
            row["duration_seconds"] = int((now - c.session_manager.started_at).total_seconds())
            row["current"] = True
    return JSONResponse(rows)


@protected
async def connection_logs(request: Request) -> JSONResponse:
    raw = request.query_params.get("limit", "100")
    if not raw.isdigit() or not 1 <= int(raw) <= 500:
        raise ValidationFailed([{"loc": ["limit"], "msg": "must be an integer between 1 and 500"}])
    platform = request.query_params.get("platform") or None
    return JSONResponse(await _c(request).repo.list_connection_logs(int(raw), platform))


# ---------------------------------------------------------------------------- AI configuration (section D)
async def _save_json(c: Any, key: str, data: Any) -> bool:
    try:
        await c.repo.set_setting(key, json.dumps(data), now_utc())
        return True
    except Exception:  # noqa: BLE001 - the change itself was applied; only remembering it failed
        logger.exception("could not save %s", key)
        return False


def _ai_payload(c: Any) -> dict[str, Any]:
    return {"values": ai_values(c.settings), "limits": {k: [lo, hi] for k, (lo, hi, _) in AI_FIELDS.items()},
            "model": c.model_manager.status()["active_model"]}


@protected
async def ai_settings(request: Request) -> JSONResponse:
    return JSONResponse(_ai_payload(_c(request)))


@protected
async def change_ai_settings(request: Request) -> JSONResponse:
    c = _c(request)
    try:
        values = parse_numbers(await _json_body(request), AI_FIELDS)
    except SettingsError as exc:
        raise ValidationFailed(exc.errors) from exc
    if values.get("reply_max_words", c.settings.reply_max_words) < c.settings.reply_target_min_words:
        raise ValidationFailed([{"loc": ["reply_max_words"], "msg": f"must not be smaller than {c.settings.reply_target_min_words}"}])
    apply_ai(c.settings, values)
    saved = await _save_json(c, "ai.params", ai_values(c.settings))
    return JSONResponse({**_ai_payload(c), "saved": saved})


@protected
async def ai_test(request: Request) -> JSONResponse:
    """Ask the model for one short sentence with the current settings, without sending or storing anything."""
    c = _c(request)
    messages = [
        {"role": "system", "content": "You are a friendly livestream chat assistant. Answer in one short sentence."},
        {"role": "user", "content": f"Say hello to the chat of {c.settings.streamer_name}'s stream and mention that the game is "
                                    f"{c.settings.current_game}."},
    ]
    try:
        result = await c.model_manager.generate(messages, num_predict=min(60, c.settings.num_predict))
    except AIUnavailable as exc:
        return JSONResponse({"detail": f"The AI is not available: {exc}"}, status_code=503)
    return JSONResponse({"text": result.text.strip()[:300], "model": result.model, "latency_ms": result.latency_ms,
                         "tokens_per_s": None if result.tokens_per_s is None else round(result.tokens_per_s, 1),
                         "used_fallback": result.used_fallback})


# ---------------------------------------------------------------------------- message limits (section F)
def _rate_payload(c: Any) -> dict[str, Any]:
    return {"platforms": {p: rate_values(cfg) for p, cfg in c.settings.rate_limits.items()},
            "limits": {k: [lo, hi] for k, (lo, hi, _) in RATE_FIELDS.items()}}


@protected
async def rate_limits(request: Request) -> JSONResponse:
    return JSONResponse(_rate_payload(_c(request)))


@protected
async def change_rate_limits(request: Request) -> JSONResponse:
    c = _c(request)
    platform = request.path_params["platform"]
    if platform not in c.settings.rate_limits:
        return JSONResponse({"detail": f"Unknown platform {platform!r}"}, status_code=404)
    try:
        values = parse_numbers(await _json_body(request), RATE_FIELDS)
    except SettingsError as exc:
        raise ValidationFailed(exc.errors) from exc
    apply_rate_limits(c.settings, platform, values)
    c.processor.apply_rate_limits(platform)
    saved = await _save_json(c, "rate_limits", {p: rate_values(cfg) for p, cfg in c.settings.rate_limits.items()})
    return JSONResponse({**_rate_payload(c), "saved": saved})


# ---------------------------------------------------------------------------- platforms and emergency controls (sections E, H)
def _adapter_or_none(c: Any, name: str):  # noqa: ANN202
    try:
        platform = Platform(name)
    except ValueError:
        return None
    return c.adapters.get(platform)


@protected
async def platform_disconnect(request: Request) -> JSONResponse:
    """Stop reading and writing on one platform (its login stays; the others keep running)."""
    c = _c(request)
    name = request.path_params["name"]
    adapter = _adapter_or_none(c, name)
    if adapter is None:
        return JSONResponse({"detail": f"Unknown platform {name!r}"}, status_code=404)
    await adapter.stop()
    return JSONResponse({"platform": name, "state": adapter.status().state.value})


@protected
async def platform_connect(request: Request) -> JSONResponse:
    c = _c(request)
    name = request.path_params["name"]
    adapter = _adapter_or_none(c, name)
    if adapter is None:
        return JSONResponse({"detail": f"Unknown platform {name!r}"}, status_code=404)
    if c.ingest is None:
        return JSONResponse({"detail": "The bot is still starting"}, status_code=503)
    await adapter.start(c.ingest)
    return JSONResponse({"platform": name, "state": adapter.status().state.value})


# ---------------------------------------------------------------------------- what the stream is about (game + background note)
@protected
async def topic_status(request: Request) -> JSONResponse:
    return JSONResponse(_c(request).topic.snapshot())


@protected
async def topic_change(request: Request) -> JSONResponse:
    """Set the game/topic and optional search words, look it up on the web, or save the streamer's own notes.

    ``{"game": "...", "hint": "...", "lookup": true}`` or ``{"notes": "..."}`` (own notes; empty text clears the note).
    A failed web lookup is not an HTTP error: the game is still changed and the problem is reported in ``error``."""
    topic = _c(request).topic
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise ValidationFailed([{"loc": ["body"], "msg": "must be an object"}])
    errors = [{"loc": [k], "msg": "unknown setting"} for k in body if k not in ("game", "hint", "notes", "lookup")]
    for key in ("game", "hint", "notes"):
        if key in body and not isinstance(body[key], str):
            errors.append({"loc": [key], "msg": "must be text"})
    if "lookup" in body and not isinstance(body["lookup"], bool):
        errors.append({"loc": ["lookup"], "msg": "must be true or false"})
    if errors:
        raise ValidationFailed(errors)
    try:
        if "game" in body:
            await topic.set_game(body["game"], body.get("hint"))
        elif "hint" in body:
            await topic.set_game(topic.snapshot()["game"], body["hint"])
        if "notes" in body:
            return JSONResponse(await topic.set_manual_notes(body["notes"]))
        if body.get("lookup", True):
            await topic.refresh()
    except TopicError as exc:
        if exc.invalid_input:
            raise ValidationFailed([{"loc": ["game"], "msg": str(exc)}]) from exc
        topic.error = str(exc)  # the game change itself worked; show why the lookup did not
    return JSONResponse(topic.snapshot())


# ---------------------------------------------------------------------------- which stream the bot comments in
def _streams(c: Any):  # noqa: ANN202
    return c.streams


def _unknown_stream(target_id: str) -> JSONResponse:
    return JSONResponse({"detail": f"Unknown stream {target_id!r}"}, status_code=404)


@protected
async def streams_status(request: Request) -> JSONResponse:
    return JSONResponse(_streams(_c(request)).snapshot())


@protected
async def streams_add(request: Request) -> JSONResponse:
    director = _streams(_c(request))
    try:
        target = await director.add_target(await _json_body(request))
    except StreamsError as exc:
        raise ValidationFailed(exc.errors) from exc
    return JSONResponse({"target": target, **director.snapshot()}, status_code=201)


@protected
async def streams_update(request: Request) -> JSONResponse:
    director = _streams(_c(request))
    target_id = request.path_params["target_id"]
    try:
        await director.update_target(target_id, await _json_body(request))
    except KeyError:
        return _unknown_stream(target_id)
    except StreamsError as exc:
        raise ValidationFailed(exc.errors) from exc
    return JSONResponse(director.snapshot())


@protected
async def streams_remove(request: Request) -> JSONResponse:
    director = _streams(_c(request))
    target_id = request.path_params["target_id"]
    try:
        await director.remove_target(target_id)
    except KeyError:
        return _unknown_stream(target_id)
    except StreamsError as exc:
        raise ValidationFailed(exc.errors) from exc
    return JSONResponse(director.snapshot())


@protected
async def streams_select(request: Request) -> JSONResponse:
    """Choose the stream the bot must be in (``{"target_id": null}`` = nothing selected: fully automatic)."""
    director = _streams(_c(request))
    body = await _json_body(request)
    if not isinstance(body, dict) or "target_id" not in body or not (body["target_id"] is None or isinstance(body["target_id"], str)):
        raise ValidationFailed([{"loc": ["target_id"], "msg": "must be a stream id or null"}])
    try:
        await director.select(body["target_id"])
    except KeyError:
        return _unknown_stream(str(body["target_id"]))
    except StreamsError as exc:
        raise ValidationFailed(exc.errors) from exc
    return JSONResponse(director.snapshot())


@protected
async def streams_settings(request: Request) -> JSONResponse:
    director = _streams(_c(request))
    try:
        return JSONResponse(await director.update_settings(await _json_body(request)))
    except StreamsError as exc:
        raise ValidationFailed(exc.errors) from exc


@protected
async def streams_check(request: Request) -> JSONResponse:
    """Look at every switched-on stream right now (uses a little YouTube quota)."""
    director = _streams(_c(request))
    await director.tick()
    return JSONResponse(director.snapshot())


@protected
async def stop_bot(request: Request) -> JSONResponse:
    """Stop (or restart) the whole bot. Needs the bot to have been started with scripts\\start.bat (python -m app.run)."""
    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is None:
        return JSONResponse({"detail": "Start the bot with scripts\\start.bat to use Stop and Restart from here"}, status_code=501)
    restart = StopRequest.parse(await _json_body(request, optional=True)).restart
    # Answer first, then shut down: the dashboard gets its reply before the server goes away.
    asyncio.get_running_loop().call_later(0.5, lambda: lifecycle.request_stop(restart=restart))
    return JSONResponse({"stopping": True, "restart": restart}, status_code=202)


# ---------------------------------------------------------------------------- manual approval (section B)
@protected
async def approvals(request: Request) -> JSONResponse:
    c = _c(request)
    await c.processor.sweep_approvals()
    return JSONResponse({"enabled": c.controls.approval_required, "timeout_s": c.processor.approvals.ttl_s,
                         "items": c.processor.approvals.public()})


@protected
async def toggle_approval(request: Request) -> JSONResponse:
    c = _c(request)
    c.controls.approval_required = AIToggle.parse(await _json_body(request)).enabled
    return JSONResponse(_controls(c))


@protected
async def approve_reply(request: Request) -> JSONResponse:
    c = _c(request)
    entry_id = request.path_params["entry_id"]
    if not entry_id.isdigit():
        raise ValidationFailed([{"loc": ["entry_id"], "msg": "must be a number"}])
    decision = ApprovalDecision.parse(await _json_body(request, optional=True))
    try:
        result = await c.processor.approve(int(entry_id), decision.text)
    except KeyError:
        return JSONResponse({"detail": "That message is no longer waiting (approved, rejected or expired)"}, status_code=404)
    except ApprovalBlocked as exc:
        return JSONResponse({"detail": exc.reason, "code": exc.code}, status_code=409)
    return JSONResponse(result.to_dict(), status_code=200 if result.action == "replied" else 502)


@protected
async def reject_reply(request: Request) -> JSONResponse:
    c = _c(request)
    entry_id = request.path_params["entry_id"]
    if not entry_id.isdigit():
        raise ValidationFailed([{"loc": ["entry_id"], "msg": "must be a number"}])
    try:
        await c.processor.reject(int(entry_id))
    except KeyError:
        return JSONResponse({"detail": "That message is no longer waiting"}, status_code=404)
    return JSONResponse({"rejected": int(entry_id)})


@protected
async def issue_ticket(request: Request) -> JSONResponse:
    return JSONResponse({"ticket": _c(request).tickets.issue(), "expires_in_s": 60}, headers={"Cache-Control": "no-store"})


async def redeem_ticket(request: Request) -> JSONResponse:
    """Public on purpose: the dashboard has no key yet. A valid one-time ticket is the credential."""
    body = TicketRedeem.parse(await _json_body(request))
    c = _c(request)
    if not c.tickets.redeem(body.ticket):
        return JSONResponse({"detail": "Invalid or expired ticket"}, status_code=401, headers={"Cache-Control": "no-store"})
    return JSONResponse({"api_key": c.api_token}, headers={"Cache-Control": "no-store"})


routes = [
    Route("/api/health", health, methods=["GET"]),
    Route("/api/scheduler", scheduler_status, methods=["GET"]),
    Route("/api/scheduler/settings", scheduler_settings, methods=["POST"]),
    Route("/api/control/proactive", toggle_proactive, methods=["POST"]),
    Route("/api/models", models, methods=["GET"]),
    Route("/api/control/model", change_model, methods=["POST"]),
    Route("/api/personalities/{name}/enabled", set_personality_enabled, methods=["POST"]),
    Route("/api/personalities/{name}/settings", personality_settings, methods=["POST"]),
    Route("/api/personalities/{name}/reset", reset_personality, methods=["POST"]),
    Route("/api/system", system_stats, methods=["GET"]),
    Route("/api/logs", logs, methods=["GET"]),
    Route("/api/sessions", sessions, methods=["GET"]),
    Route("/api/connection-logs", connection_logs, methods=["GET"]),
    Route("/api/ai/settings", ai_settings, methods=["GET"]),
    Route("/api/ai/settings", change_ai_settings, methods=["POST"]),
    Route("/api/ai/test", ai_test, methods=["POST"]),
    Route("/api/rate-limits", rate_limits, methods=["GET"]),
    Route("/api/rate-limits/{platform}", change_rate_limits, methods=["POST"]),
    Route("/api/platforms/{name}/disconnect", platform_disconnect, methods=["POST"]),
    Route("/api/platforms/{name}/connect", platform_connect, methods=["POST"]),
    Route("/api/topic", topic_status, methods=["GET"]),
    Route("/api/topic", topic_change, methods=["POST"]),
    Route("/api/streams", streams_status, methods=["GET"]),
    Route("/api/streams/targets", streams_add, methods=["POST"]),
    Route("/api/streams/targets/{target_id}", streams_update, methods=["POST"]),
    Route("/api/streams/targets/{target_id}/remove", streams_remove, methods=["POST"]),
    Route("/api/streams/select", streams_select, methods=["POST"]),
    Route("/api/streams/settings", streams_settings, methods=["POST"]),
    Route("/api/streams/check", streams_check, methods=["POST"]),
    Route("/api/control/stop", stop_bot, methods=["POST"]),
    Route("/api/approvals", approvals, methods=["GET"]),
    Route("/api/approvals/{entry_id}/approve", approve_reply, methods=["POST"]),
    Route("/api/approvals/{entry_id}/reject", reject_reply, methods=["POST"]),
    Route("/api/control/approval", toggle_approval, methods=["POST"]),
    Route("/api/auth/ticket", issue_ticket, methods=["POST"]),
    Route("/api/auth/redeem", redeem_ticket, methods=["POST"]),
    Route("/api/simulate/message", simulate_message, methods=["POST"]),
    Route("/api/messages", messages, methods=["GET"]),
    Route("/api/personalities", personalities, methods=["GET"]),
    Route("/api/personalities/{name}/preview", preview, methods=["POST"]),
    Route("/api/status", status, methods=["GET"]),
    Route("/api/control/pause", pause, methods=["POST"]),
    Route("/api/control/resume", resume, methods=["POST"]),
    Route("/api/control/ai", toggle_ai, methods=["POST"]),
]
