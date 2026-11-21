import httpx
import pytest

from app.ai.model_manager import AIUnavailable, ModelManager, parse_duration_s
from app.ai.ollama_client import OllamaClient
from tests.conftest import FakeClock

MSGS = [{"role": "user", "content": "hello"}]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mm(settings, fake_ollama, clock) -> ModelManager:
    client = OllamaClient(settings.ollama_host, transport=httpx.MockTransport(fake_ollama.handler))
    return ModelManager(client, settings, clock=clock)


def test_parse_duration():
    assert parse_duration_s("30m") == 1800 and parse_duration_s("90s") == 90 and parse_duration_s("2h") == 7200
    assert parse_duration_s("-1") == float("inf") and parse_duration_s("45") == 45


async def test_primary_success(mm, fake_ollama):
    res = await mm.generate(MSGS)
    assert res.model == "primary-model" and not res.used_fallback and res.text
    assert fake_ollama.real_calls[0]["options"]["num_gpu"] == 99  # Phase 1: all layers on the GPU
    assert fake_ollama.real_calls[0]["think"] is False


async def test_failing_primary_retries_once_then_uses_fallback_then_recovers(mm, fake_ollama, clock):
    fake_ollama.fail_models["primary-model"] = "500"
    res = await mm.generate(MSGS)
    assert res.model == "fallback-model" and res.used_fallback
    assert [c["model"] for c in fake_ollama.real_calls].count("primary-model") == 2  # first try + ONE retry

    fake_ollama.chat_calls.clear()
    again = await mm.generate(MSGS)  # primary is skipped while it is marked degraded
    assert again.model == "fallback-model"
    assert {c["model"] for c in fake_ollama.real_calls} == {"fallback-model"}
    assert mm.status()["primary_degraded"] is True

    clock.advance(61)  # re-check interval passed and the primary is healthy again
    fake_ollama.fail_models.clear()
    fake_ollama.chat_calls.clear()
    assert (await mm.generate(MSGS)).model == "primary-model"
    assert mm.status()["primary_degraded"] is False


async def test_forced_gpu_failure_is_retried_with_automatic_placement(mm, fake_ollama):
    fake_ollama.fail_models["primary-model"] = "gpu500"  # e.g. VRAM taken by another app
    res = await mm.generate(MSGS)
    first, second = fake_ollama.real_calls[:2]
    assert "num_gpu" in first["options"] and "num_gpu" not in second["options"]
    assert res.model == "primary-model" and not res.used_fallback


async def test_ollama_down_backs_off_exponentially_without_hammering(mm, fake_ollama, clock):
    fake_ollama.version_ok = False
    with pytest.raises(AIUnavailable):
        await mm.generate(MSGS)
    n = len(fake_ollama.requests)
    with pytest.raises(AIUnavailable):  # still backing off: no HTTP traffic at all
        await mm.generate(MSGS)
    assert len(fake_ollama.requests) == n

    clock.advance(5.1)  # first backoff (5 s) over: one more probe, which fails and doubles the wait to 10 s
    with pytest.raises(AIUnavailable):
        await mm.generate(MSGS)
    n = len(fake_ollama.requests)
    assert n > 0
    clock.advance(6)
    with pytest.raises(AIUnavailable):
        await mm.generate(MSGS)
    assert len(fake_ollama.requests) == n  # 6 s < 10 s: still no traffic
    clock.advance(4.1)
    with pytest.raises(AIUnavailable):
        await mm.generate(MSGS)
    assert len(fake_ollama.requests) > n


async def test_health_check_detects_recovery_and_clears_backoff(mm, fake_ollama, clock):
    fake_ollama.version_ok = False
    with pytest.raises(AIUnavailable):
        await mm.generate(MSGS)
    assert (await mm.health_check())["ollama_up"] is False
    fake_ollama.version_ok = True
    health = await mm.health_check()
    assert health["ollama_up"] and health["primary_installed"] and health["fallback_installed"]
    assert (await mm.generate(MSGS)).model == "primary-model"  # no need to wait out the backoff


async def test_all_models_failing_raises_unavailable(mm, fake_ollama):
    fake_ollama.fail_models.update({"primary-model": "500", "fallback-model": "timeout"})
    with pytest.raises(AIUnavailable, match="all models failed"):
        await mm.generate(MSGS)


async def test_missing_model_falls_back(mm, fake_ollama):
    fake_ollama.fail_models["primary-model"] = "404"
    assert (await mm.generate(MSGS)).model == "fallback-model"


async def test_no_fallback_configured(make_settings, fake_ollama, clock):
    s = make_settings(fallback_model=None)
    mm = ModelManager(OllamaClient(s.ollama_host, transport=httpx.MockTransport(fake_ollama.handler)), s, clock=clock)
    fake_ollama.fail_models["primary-model"] = "500"
    with pytest.raises(AIUnavailable):
        await mm.generate(MSGS)
    with pytest.raises(AIUnavailable, match="no fallback"):
        await mm.generate(MSGS)  # primary skipped, nothing else to try


async def test_warm_up_uses_the_same_options_as_real_requests(mm, fake_ollama):
    assert await mm.warm_up() is True
    warm = fake_ollama.chat_calls[0]
    assert warm["options"]["num_ctx"] == 4096 and warm["options"]["num_gpu"] == 99  # else Ollama reloads the model
