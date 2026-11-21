"""Thin async client for Ollama's local HTTP API (https://docs.ollama.com/api). Nothing else talks to Ollama."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OllamaError(Exception):
    """Base class for every failure talking to Ollama."""


class OllamaUnavailable(OllamaError):
    """Cannot connect: Ollama is not running (or the connection dropped)."""


class OllamaTimeout(OllamaError):
    pass


class OllamaModelMissing(OllamaError):
    pass


class OllamaServerError(OllamaError):
    """Ollama answered with an error status or an unusable body."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    thinking: str
    model: str
    eval_count: int
    eval_duration_ns: int
    prompt_eval_count: int
    load_duration_ns: int
    total_duration_ns: int
    done_reason: str | None

    @property
    def tokens_per_second(self) -> float | None:
        return self.eval_count / (self.eval_duration_ns / 1e9) if self.eval_duration_ns else None


class OllamaClient:
    def __init__(self, host: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # trust_env=False: never route loopback traffic through a system proxy
        self._http = httpx.AsyncClient(
            base_url=host.rstrip("/"), transport=transport, trust_env=False,
            timeout=httpx.Timeout(10.0, connect=3.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, json: dict | None = None, timeout: float) -> dict[str, Any]:
        try:
            async with asyncio.timeout(timeout):  # hard wall-clock deadline for the whole call
                resp = await self._http.request(method, path, json=json, timeout=timeout)
        except TimeoutError as exc:
            raise OllamaTimeout(f"{path} exceeded {timeout:.0f}s") from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeout(f"{path} timed out") from exc
        except httpx.TransportError as exc:
            raise OllamaUnavailable(f"cannot reach Ollama: {exc.__class__.__name__}") from exc
        if resp.status_code == 404:
            raise OllamaModelMissing(resp.text[:200])
        if resp.status_code >= 400:
            raise OllamaServerError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise OllamaServerError("response was not JSON") from exc

    async def version(self, timeout: float = 3.0) -> str:
        return str((await self._request("GET", "/api/version", timeout=timeout)).get("version", "unknown"))

    async def list_models(self, timeout: float = 5.0) -> list[str]:
        data = await self._request("GET", "/api/tags", timeout=timeout)
        return [m["name"] for m in data.get("models", []) if "name" in m]

    async def running_models(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        return list((await self._request("GET", "/api/ps", timeout=timeout)).get("models", []))

    async def chat(
        self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any], keep_alive: str,
        think: bool = False, timeout: float,
    ) -> ChatResult:
        payload = {
            "model": model, "messages": messages, "stream": False, "keep_alive": keep_alive,
            "think": think, "options": options,
        }
        data = await self._request("POST", "/api/chat", json=payload, timeout=timeout)
        message = data.get("message") or {}
        return ChatResult(
            content=message.get("content") or "",
            thinking=message.get("thinking") or "",
            model=model,
            eval_count=int(data.get("eval_count") or 0),
            eval_duration_ns=int(data.get("eval_duration") or 0),
            prompt_eval_count=int(data.get("prompt_eval_count") or 0),
            load_duration_ns=int(data.get("load_duration") or 0),
            total_duration_ns=int(data.get("total_duration") or 0),
            done_reason=data.get("done_reason"),
        )

    async def unload(self, model: str, timeout: float = 10.0) -> None:
        await self._request("POST", "/api/generate", json={"model": model, "keep_alive": 0}, timeout=timeout)
