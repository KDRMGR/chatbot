"""Simulated platform: captures what the bot would have posted. Used for development and tests."""
from __future__ import annotations

import itertools
from collections import deque

from ..chat.models import ChatMessage, Platform
from ..chat.normalizer import now_utc
from .base import ConnectionState, ConnectionStatus, OnMessage, PlatformAdapter, SendResult


class SimulatorAdapter(PlatformAdapter):
    platform = Platform.SIMULATOR

    def __init__(self) -> None:
        self.sent: deque[str] = deque(maxlen=200)
        self.fail_next: str | None = None  # tests: make the next send fail with this error
        self._ids = itertools.count(1)
        self._status = ConnectionStatus()

    async def start(self, on_message: OnMessage) -> None:
        self._status = ConnectionStatus(ConnectionState.CONNECTED, "simulator", now_utc())

    async def stop(self) -> None:
        self._status = ConnectionStatus(ConnectionState.DISCONNECTED, None, now_utc())

    async def send(self, text: str, *, reply_to: ChatMessage | None = None) -> SendResult:
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            return SendResult(ok=False, error=error, retryable=True)
        self.sent.append(text)
        return SendResult(ok=True, platform_message_id=f"sim-out-{next(self._ids)}")

    def status(self) -> ConnectionStatus:
        return self._status
