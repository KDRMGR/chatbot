"""Fault-injecting TCP proxy for reliability tests (Phase 7). Sits between the bot and Ollama (real or fake) and can
break the connection in the ways a real outage does, WITHOUT touching the real Ollama process.

    python scripts/fault_proxy.py --listen 11500 --backend 127.0.0.1:11434 --control state/proxy.json

The control file is re-read twice a second; write JSON like {"mode": "refuse", "delay_s": 0}:

    up       forward normally
    refuse   stop listening: connections are REFUSED (like Ollama not running)
    reset    accept, then drop the connection at once (like a crash while serving)
    hang     accept and swallow the request but never answer (like a stalled GPU or a frozen server)
    delay_s  seconds of extra latency before forwarding (any mode that forwards)

Standard library only. Loopback only. Development tool: the bot itself never imports this.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


class FaultProxy:
    def __init__(self, listen_port: int, backend_host: str, backend_port: int, control: Path) -> None:
        self.listen_port, self.backend_host, self.backend_port, self.control = listen_port, backend_host, backend_port, control
        self.mode, self.delay_s = "up", 0.0
        self.server: asyncio.AbstractServer | None = None
        self.writers: set[asyncio.StreamWriter] = set()
        self.stats = {"accepted": 0, "forwarded": 0, "reset": 0, "hung": 0}

    async def handle(self, creader: asyncio.StreamReader, cwriter: asyncio.StreamWriter) -> None:
        self.writers.add(cwriter)
        self.stats["accepted"] += 1
        try:
            if self.mode == "reset":
                self.stats["reset"] += 1
                cwriter.transport.abort()
                return
            if self.mode == "hang":
                self.stats["hung"] += 1
                while await creader.read(4096):  # swallow whatever arrives; never answer; end when the client gives up
                    pass
                return
            if self.delay_s > 0:
                await asyncio.sleep(self.delay_s)
            try:
                breader, bwriter = await asyncio.open_connection(self.backend_host, self.backend_port)
            except OSError:
                cwriter.transport.abort()
                return
            self.stats["forwarded"] += 1
            await asyncio.gather(_pipe(creader, bwriter), _pipe(breader, cwriter))
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            self.writers.discard(cwriter)
            try:
                cwriter.close()
            except Exception:  # noqa: BLE001
                pass

    async def _listen(self) -> None:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", self.listen_port)

    async def _stop_listening(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None
        for writer in list(self.writers):  # a dead server drops its open connections too
            try:
                writer.transport.abort()
            except Exception:  # noqa: BLE001
                pass

    async def run(self) -> None:
        await self._listen()
        print(f"fault proxy: 127.0.0.1:{self.listen_port} -> {self.backend_host}:{self.backend_port}, control={self.control}", flush=True)
        while True:
            try:
                data = json.loads(self.control.read_text(encoding="utf-8")) if self.control.exists() else {}
            except (ValueError, OSError):
                data = {}
            mode = str(data.get("mode", "up"))
            self.delay_s = float(data.get("delay_s", 0) or 0)
            if mode != self.mode:
                print(f"fault proxy: mode {self.mode} -> {mode}", flush=True)
                if mode == "refuse":
                    await self._stop_listening()
                elif self.mode == "refuse":
                    await self._listen()
                self.mode = mode
            await asyncio.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--backend", required=True, help="host:port of the real (or fake) Ollama")
    parser.add_argument("--control", type=Path, required=True)
    args = parser.parse_args()
    host, _, port = args.backend.rpartition(":")
    if host not in ("127.0.0.1", "localhost"):
        print("refusing: the backend must be on this machine (loopback)", file=sys.stderr)
        return 2
    try:
        asyncio.run(FaultProxy(args.listen, host, int(port), args.control).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
