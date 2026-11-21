"""Platform adapter contract. Phase 3 (YouTube) and Phase 4 (Kick) implement this; Phase 2 ships only the
simulator. The AI never sends anything itself: the processor hands finished, validated text to an adapter."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable

from ..chat.models import ChatMessage, Platform


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class ConnectionStatus:
    state: ConnectionState = ConnectionState.DISCONNECTED
    detail: str | None = None
    since: datetime | None = None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    platform_message_id: str | None = None
    error: str | None = None
    retryable: bool = False


OnMessage = Callable[[ChatMessage], Awaitable[Any]]


class PlatformAdapter(ABC):
    platform: Platform

    @abstractmethod
    async def start(self, on_message: OnMessage) -> None:
        """Connect and begin delivering normalised ChatMessages to ``on_message``."""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, text: str, *, reply_to: ChatMessage | None = None) -> SendResult:
        """Send one finished message. Must not raise for ordinary failures; return SendResult(ok=False, ...)."""

    @abstractmethod
    def status(self) -> ConnectionStatus: ...
