"""Model selection, health and failure handling (spec 4.4).

Order on failure:  retry once -> check Ollama is alive -> fallback model -> AIUnavailable.
The caller (processor) then falls back to rule-based replies, so the bot never depends on the AI.
One generation at a time (a semaphore) keeps a 4 GB GPU and a live chat both predictable.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from ..config import Settings
from .ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaModelMissing,
    OllamaServerError,
    OllamaTimeout,
    OllamaUnavailable,
)

logger = logging.getLogger(__name__)


class AIUnavailable(Exception):
    """No model could produce a reply right now."""


class ModelSwitchRejected(Exception):
    """A runtime model change was refused or failed; the previous model stays active.

    ``code`` is one of: not_installed, ollama_down, busy, failed, invalid, unchanged.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


_SWITCH_TEST = [
    {"role": "system", "content": "You are a friendly livestream chat assistant. Answer in one short sentence."},
    {"role": "user", "content": "Welcome a new viewer to the stream."},
]


@dataclass(frozen=True)
class AIResult:
    text: str
    model: str
    latency_ms: int
    eval_count: int
    tokens_per_s: float | None
    used_fallback: bool
    thinking_leaked: bool


def parse_duration_s(value: str) -> float:
    """'30m' -> 1800, '90s' -> 90, '2h' -> 7200, '-1' -> never expires (inf)."""
    value = value.strip().lower()
    if value.startswith("-"):
        return float("inf")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", value)
    if not match:
        return 300.0
    return float(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


class ModelManager:
    def __init__(self, client: OllamaClient, settings: Settings, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._s = settings
        self._clock = clock
        self._sem = asyncio.Semaphore(1)
        self._primary_bad_until = 0.0
        self._down_until = 0.0
        self._down_backoff = 5.0
        self._last_success: float | None = None
        self._keep_alive_s = parse_duration_s(settings.keep_alive)
        self._health: dict[str, Any] = {"ollama_up": None}
        # The models in use. They start as the configured ones and can be changed at runtime (dashboard) without
        # touching ``settings`` (which stays exactly what the user configured).
        self._primary = settings.primary_model
        self._fallback = settings.fallback_model
        self._switching_to: str | None = None
        self._look_task: asyncio.Future[Any] | None = None
        self.counters = {"generations": 0, "failures": 0, "fallback_used": 0}

    @property
    def primary(self) -> str:
        return self._primary

    @property
    def fallback(self) -> str | None:
        return self._fallback

    @property
    def busy(self) -> bool:
        """True while a reply is being written (background jobs wait their turn: viewers always come first)."""
        return self._sem.locked()

    async def look(self, image: bytes, prompt: str, *, model: str | None = None, num_predict: int = 60) -> str | None:
        """Describe a picture with the (vision-capable) model. Returns None when the model is busy with a reply: a look is never
        worth keeping a viewer waiting. Uses the primary model only (the backup may not be able to see). Raises AIUnavailable."""
        if self._sem.locked() or self._clock() < self._down_until:
            return None
        async with self._sem:
            name = model or self._primary
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt, "images": [base64.b64encode(image).decode()]}]
            # A NEW picture takes several seconds on this graphics card (an identical one is cached and looks instant). So the look
            # runs as its own task that ``interrupt_look`` can cancel: cancelling closes the connection and Ollama stops, which frees
            # the model for the viewer who just wrote.
            task = asyncio.ensure_future(self._client.chat(
                name, messages, options=self._options(0.3, num_predict, self._s.num_gpu), keep_alive=self._s.keep_alive,
                timeout=self._s.generation_timeout_s))
            self._look_task = task
            try:
                result = await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if task.cancelled() and not (current is not None and current.cancelling()):
                    self.counters["looks_interrupted"] = self.counters.get("looks_interrupted", 0) + 1
                    return None  # interrupted for a viewer: not an error
                task.cancel()
                raise
            except OllamaError as exc:
                raise AIUnavailable(f"the model could not look at the picture ({exc.__class__.__name__})") from exc
            finally:
                self._look_task = None
            self._last_success = self._clock()
            return result.content

    def interrupt_look(self) -> bool:
        """A viewer wrote: stop looking at a picture right now if a look is running. Returns True if one was cancelled."""
        task = self._look_task
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    # ------------------------------------------------------------------ generation
    def _options(self, temperature: float | None, num_predict: int | None, num_gpu: int | None) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "num_ctx": self._s.num_ctx,
            "num_predict": num_predict or self._s.num_predict,
            "temperature": self._s.temperature if temperature is None else temperature,
        }
        if num_gpu is not None:
            opts["num_gpu"] = num_gpu
        return opts

    def _expected_cold(self) -> bool:
        """True if the model has probably been unloaded (never used yet, or idle past keep_alive)."""
        if self._last_success is None:
            return True
        return (self._clock() - self._last_success) > self._keep_alive_s * 0.9

    def _mark_down(self) -> None:
        self._down_until = self._clock() + self._down_backoff
        logger.error("Ollama unreachable; AI paused for %.0fs (rule-based replies continue)", self._down_backoff)
        self._down_backoff = min(self._down_backoff * 2, 60.0)  # exponential backoff, capped

    async def _ollama_running(self) -> bool:
        try:
            await self._client.version(timeout=2.0)
            return True
        except OllamaError:
            return False

    async def generate(
        self, messages: list[dict[str, str]], *, temperature: float | None = None, num_predict: int | None = None
    ) -> AIResult:
        s = self._s
        if self._clock() < self._down_until:
            raise AIUnavailable("Ollama unreachable (backing off)")

        async with self._sem:
            # Decided AFTER getting the turn: a model switch may have happened while this call was waiting.
            if self._clock() < self._down_until:
                raise AIUnavailable("Ollama unreachable (backing off)")
            candidates: list[str] = []
            if self._clock() >= self._primary_bad_until:
                candidates.append(self._primary)
            if self._fallback and self._fallback != self._primary:
                candidates.append(self._fallback)
            if not candidates:
                raise AIUnavailable("primary model is failing and no fallback is configured")
            cold = self._expected_cold()
            per_attempt = s.warmup_timeout_s if cold else s.generation_timeout_s
            deadline = self._clock() + (s.warmup_timeout_s if cold else s.generation_deadline_s)
            last_error: Exception | None = None
            for model in candidates:
                try:
                    result = await self._attempt(model, messages, temperature, num_predict, per_attempt, deadline)
                except OllamaUnavailable as exc:
                    self._mark_down()
                    raise AIUnavailable("Ollama unreachable") from exc
                except OllamaError as exc:
                    last_error = exc
                    self.counters["failures"] += 1
                    if model == self._primary:
                        self._primary_bad_until = self._clock() + s.model_recheck_interval_s
                        logger.warning("Primary model %s failing (%s); skipping it for %.0fs",
                                       model, exc, s.model_recheck_interval_s)
                    if not await self._ollama_running():
                        self._mark_down()
                        raise AIUnavailable("Ollama stopped responding") from exc
                    continue
                self._last_success = self._clock()
                self._down_backoff = 5.0
                self.counters["generations"] += 1
                if model != self._primary:
                    self.counters["fallback_used"] += 1
                    result = replace(result, used_fallback=True)
                else:
                    self._primary_bad_until = 0.0
                return result
            raise AIUnavailable(f"all models failed: {last_error}")

    async def _attempt(
        self, model: str, messages: list[dict[str, str]], temperature: float | None, num_predict: int | None,
        per_attempt: float, deadline: float,
    ) -> AIResult:
        num_gpu = self._s.num_gpu
        last: OllamaError | None = None
        for attempt in (1, 2):  # "retry once"
            remaining = deadline - self._clock()
            if remaining < 1.0:
                break
            started = self._clock()
            try:
                res = await self._client.chat(
                    model, messages, options=self._options(temperature, num_predict, num_gpu),
                    keep_alive=self._s.keep_alive, think=False, timeout=min(per_attempt, remaining),
                )
            except (OllamaModelMissing, OllamaUnavailable):
                raise
            except (OllamaServerError, OllamaTimeout) as exc:
                last = exc
                logger.warning("Attempt %d on %s failed: %s", attempt, model, exc)
                if isinstance(exc, OllamaServerError) and num_gpu is not None:
                    num_gpu = None  # forced GPU placement may be what failed (e.g. VRAM taken): let Ollama decide
                continue
            leaked = bool(res.thinking) or "<think>" in res.content.lower()
            if leaked:
                logger.warning("Model %s emitted thinking output despite think=false", model)
            return AIResult(
                text=res.content, model=model, latency_ms=int((self._clock() - started) * 1000),
                eval_count=res.eval_count, tokens_per_s=res.tokens_per_second, used_fallback=False,
                thinking_leaked=leaked,
            )
        raise last or OllamaTimeout("generation deadline exhausted")

    # ------------------------------------------------------------------ runtime model changes (dashboard)
    async def _installed_or_reject(self) -> list[str]:
        try:
            return await self._client.list_models()
        except OllamaError as exc:
            raise ModelSwitchRejected("ollama_down", f"Ollama is not reachable: {exc}") from exc

    async def switch_primary(self, name: str) -> dict[str, Any]:
        """Make ``name`` the primary model, but only after it has really produced a reply.

        The candidate is loaded and asked for one short sentence with the SAME options real replies use (so Ollama
        does not have to reload it for the first viewer). If anything fails, the previous model stays in charge.
        Chat replies wait (they are delayed, not lost) while the swap is in progress.
        """
        name = (name or "").strip()
        if not name or len(name) > 200:
            raise ModelSwitchRejected("invalid", "model name must be 1-200 characters")
        installed = await self._installed_or_reject()
        if name not in installed:
            raise ModelSwitchRejected(
                "not_installed", f"{name!r} is not installed in Ollama. Install it first with: ollama pull {name}")
        if name == self._primary:
            raise ModelSwitchRejected("unchanged", f"{name!r} is already the primary model")

        timeout = self._s.model_switch_timeout_s
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ModelSwitchRejected("busy", "the AI is busy answering chat; try again in a moment") from exc
        previous = self._primary
        self._switching_to = name
        try:
            try:
                result = await self._attempt(
                    name, _SWITCH_TEST, 0.4, 32, timeout, self._clock() + timeout)
            except OllamaModelMissing as exc:
                raise ModelSwitchRejected("not_installed", f"{name!r} is not installed in Ollama") from exc
            except OllamaUnavailable as exc:
                raise ModelSwitchRejected("ollama_down", "Ollama stopped responding during the test") from exc
            except OllamaError as exc:
                raise ModelSwitchRejected(
                    "failed", f"{name!r} failed its test reply ({exc}); {previous!r} stays active") from exc
            finally:
                # Loading the candidate may have pushed the old model out of the 4 GB GPU: treat it as cold.
                self._last_success = None
            if not result.text.strip():
                raise ModelSwitchRejected("failed", f"{name!r} returned an empty reply; {previous!r} stays active")
            self._primary, self._primary_bad_until = name, 0.0
            self._last_success = self._clock()  # the new model is loaded and warm
            self._down_until, self._down_backoff = 0.0, 5.0
            fallback_changed = self._fallback == name
            if fallback_changed:  # the old backup became the primary: keep a backup by promoting the old primary
                self._fallback = previous
        finally:
            self._switching_to = None
            self._sem.release()
        try:  # free the GPU of the model we no longer use (best effort; Ollama would evict it anyway when needed)
            await self._client.unload(previous)
        except OllamaError:
            pass
        logger.info("Primary model switched %s -> %s (%d ms)", previous, name, result.latency_ms)
        return {
            "primary": name, "previous": previous, "latency_ms": result.latency_ms,
            "tokens_per_s": None if result.tokens_per_s is None else round(result.tokens_per_s, 1),
            "sample": result.text.strip()[:200], "thinking_leaked": result.thinking_leaked,
            "fallback": self._fallback, "fallback_changed": fallback_changed,
        }

    async def set_fallback(self, name: str | None) -> dict[str, Any]:
        """Choose the backup model (``None`` = no backup, rule-based replies take over instead).

        It is only checked for being installed: loading it for a test would push the primary out of a 4 GB GPU.
        It is used (and so really tested) the first time the primary fails.
        """
        name = (name or "").strip() or None
        if name is not None:
            if len(name) > 200:
                raise ModelSwitchRejected("invalid", "model name must be 1-200 characters")
            if name == self._primary:
                raise ModelSwitchRejected("invalid", "the fallback must be a different model than the primary")
            if name not in await self._installed_or_reject():
                raise ModelSwitchRejected(
                    "not_installed", f"{name!r} is not installed in Ollama. Install it first with: ollama pull {name}")
        self._fallback = name
        logger.info("Fallback model set to %s", name)
        return {"fallback": name}

    async def restore(self, primary: str | None, fallback: str | None, *, have_fallback_choice: bool) -> None:
        """Re-apply the choices saved by the dashboard at startup. A saved model that is no longer installed is
        ignored (the configured one stays), so a deleted model can never leave the bot without an AI."""
        try:
            installed = await self._client.list_models()
        except OllamaError:
            installed = None  # cannot check right now: trust the saved choice; failures degrade to the fallback
        if primary and primary != self._primary and (installed is None or primary in installed):
            self._primary = primary
            logger.info("Using the primary model chosen in the dashboard: %s", primary)
        if have_fallback_choice and fallback != self._primary and (
            fallback is None or installed is None or fallback in installed
        ):
            self._fallback = fallback
        if self._fallback == self._primary:  # e.g. the saved primary is the configured backup: no backup left
            self._fallback = None

    # ------------------------------------------------------------------ health
    async def health_check(self) -> dict[str, Any]:
        try:
            version = await self._client.version()
            installed = await self._client.list_models()
            loaded = [
                {
                    "name": m.get("name"), "size": m.get("size", 0), "size_vram": m.get("size_vram", 0),
                    "gpu_pct": round(100 * m.get("size_vram", 0) / m["size"], 1) if m.get("size") else None,
                }
                for m in await self._client.running_models()
            ]
        except OllamaError as exc:
            self._health = {"ollama_up": False, "error": str(exc)}
            return self._health
        if self._down_until:  # recovered: stop backing off
            logger.info("Ollama is reachable again")
            self._down_until, self._down_backoff = 0.0, 5.0
        self._health = {
            "ollama_up": True, "version": version, "installed": installed, "loaded": loaded,
            "primary_installed": self._primary in installed,
            "fallback_installed": bool(self._fallback) and self._fallback in installed,
        }
        return self._health

    async def warm_up(self) -> bool:
        """Load the model before the first viewer message so nobody waits for a cold start."""
        health = await self.health_check()
        if not health.get("ollama_up"):
            logger.warning("Warm-up skipped: Ollama is not reachable")
            return False
        model = self._primary if health["primary_installed"] else self._fallback
        if not model or model not in health["installed"]:
            logger.error("Warm-up skipped: configured models are not installed (installed: %s)", health["installed"])
            return False
        try:
            # Same num_ctx / num_gpu as real requests, otherwise Ollama would reload the model for the first reply.
            await self._client.chat(
                model, [{"role": "user", "content": "Say hi."}],
                options=self._options(None, 1, self._s.num_gpu), keep_alive=self._s.keep_alive,
                think=False, timeout=self._s.warmup_timeout_s,
            )
        except OllamaError as exc:
            logger.warning("Warm-up of %s failed: %s", model, exc)
            return False
        self._last_success = self._clock()
        logger.info("Model %s warmed up", model)
        return True

    async def run_health_loop(self) -> None:
        while True:
            try:
                await self.health_check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a health probe must never kill the loop
                logger.exception("health check crashed")
            await asyncio.sleep(self._s.health_interval_s)

    def status(self) -> dict[str, Any]:
        now = self._clock()
        return {
            **self._health,
            "primary_model": self._primary,
            "fallback_model": self._fallback,
            "switching_to": self._switching_to,
            "active_model": (
                self._primary if now >= self._primary_bad_until or not self._fallback else self._fallback
            ),
            "primary_degraded": now < self._primary_bad_until,
            "backing_off": now < self._down_until,
            "seconds_since_last_success": None if self._last_success is None else round(now - self._last_success, 1),
            "counters": dict(self.counters),
        }
