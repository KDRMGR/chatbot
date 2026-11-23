"""A picture of what viewers see, from OBS Studio's built-in WebSocket server (obs-websocket 5.x, shipped with OBS 28 and newer).

Why OBS: its program output is exactly the stream, without any private window next to it. It is on this PC's loopback address only.
Turn it on in OBS: Tools > WebSocket Server Settings > Enable (port 4455); if you set a password, store it once with
``scripts\\obs_login.py`` (encrypted for this Windows user, like the other logins).

The protocol (https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md): the server says Hello (op 0, with a
challenge when a password is set), the client Identifies (op 1) and then sends requests (op 6) that are answered by op 7. The requests
used are GetVersion, GetCurrentProgramScene and GetSourceScreenshot. This file also contains the few lines of WebSocket framing needed
(RFC 6455, client side) so that no extra package has to be installed. One short connection per look: nothing stays open.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
from typing import Any, Awaitable, Callable

CREDENTIAL_KEY = "obs_websocket"  # {"password": ...}, encrypted for this Windows user (scripts\obs_login.py)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 12 * 1024 * 1024
Opener = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class ObsError(Exception):
    """OBS could not be asked; the message is safe to show in the panel."""


def auth_string(password: str, salt: str, challenge: str) -> str:
    """The Identify answer to OBS's challenge (documented): base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def encode_frame(opcode: int, payload: bytes, mask_key: bytes | None = None) -> bytes:
    """One masked client frame (clients must mask)."""
    mask_key = mask_key or os.urandom(4)
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload)) if n < 4096 else _mask_big(payload, mask_key)
    return head + mask_key + masked


def _mask_big(payload: bytes, key: bytes) -> bytes:
    # XOR with a repeating 4-byte key using big integers: fast enough for large frames in pure Python
    repeated = (key * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(payload), "big")


async def read_message(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[int, bytes]:
    """The next complete data message (text or binary); answers pings, ends on a close frame."""
    opcode, data = 0, b""
    while True:
        b1, b2 = await reader.readexactly(2)
        fin, op = bool(b1 & 0x80), b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await reader.readexactly(8))[0]
        if length > MAX_MESSAGE_BYTES or len(data) + length > MAX_MESSAGE_BYTES:
            raise ObsError("OBS sent a message that is too large")
        mask = await reader.readexactly(4) if b2 & 0x80 else None
        payload = await reader.readexactly(length)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if op == 8:
            raise ObsError("OBS closed the connection")
        if op == 9:
            writer.write(encode_frame(10, payload))
            await writer.drain()
            continue
        if op == 10:
            continue
        if op in (1, 2):
            opcode, data = op, payload
        elif op == 0:
            data += payload
        if fin:
            return opcode, data


class ObsClient:
    def __init__(self, host: str, port: int, load_password: Callable[[], str | None], *, opener: Opener | None = None,
                 timeout_s: float = 6.0) -> None:
        self._host, self._port, self._load_password = host, port, load_password
        self._open = opener or (lambda h, p: asyncio.open_connection(h, p))
        self._timeout = timeout_s

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((f"GET / HTTP/1.1\r\nHost: {self._host}:{self._port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        await writer.drain()
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = head.split("\r\n")
        if " 101 " not in lines[0]:
            raise ObsError("OBS did not accept the connection (is the WebSocket server switched on?)")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)}
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ObsError("OBS gave an unexpected answer to the connection request")

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        writer.write(encode_frame(1, json.dumps(message).encode()))
        await writer.drain()

    @staticmethod
    async def _next(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, want_op: int, request_id: str | None = None) -> dict[str, Any]:
        while True:
            _, data = await read_message(reader, writer)
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("op") == want_op:
                if request_id is None or (message.get("d") or {}).get("requestId") == request_id:
                    return message

    async def _request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, kind: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = os.urandom(6).hex()
        await self._send(writer, {"op": 6, "d": {"requestType": kind, "requestId": request_id, **({"requestData": data} if data else {})}})
        answer = (await self._next(reader, writer, 7, request_id)).get("d") or {}
        status = answer.get("requestStatus") or {}
        if not status.get("result"):
            raise ObsError(f"OBS refused {kind}: {str(status.get('comment') or status.get('code') or 'no reason given')[:80]}")
        return answer.get("responseData") or {}

    async def screenshot(self, max_side: int = 960) -> bytes:
        """The program output as image bytes (JPEG when OBS offers it, else PNG)."""
        unreachable = ObsError(f"OBS is not reachable (is it running with the WebSocket server on, port {self._port}?)")
        try:
            try:  # on Windows a closed port can take a few seconds to say so: that is "not reachable", not "slow"
                reader, writer = await asyncio.wait_for(self._open(self._host, self._port), timeout=min(3.0, self._timeout))
            except (TimeoutError, OSError) as exc:
                raise unreachable from exc
            async with asyncio.timeout(self._timeout):
                try:
                    await self._handshake(reader, writer)
                    hello = (await self._next(reader, writer, 0)).get("d") or {}
                    identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}  # no events: only answers
                    auth = hello.get("authentication")
                    if auth:
                        password = self._load_password()
                        if not password:
                            raise ObsError("OBS asks for a password: store it with scripts\\obs_login.py")
                        identify["authentication"] = auth_string(password, str(auth.get("salt", "")), str(auth.get("challenge", "")))
                    await self._send(writer, {"op": 1, "d": identify})
                    await self._next(reader, writer, 2)
                    version = await self._request(reader, writer, "GetVersion")
                    fmt = "jpg" if "jpg" in (version.get("supportedImageFormats") or []) else "png"
                    scene = (await self._request(reader, writer, "GetCurrentProgramScene")).get("currentProgramSceneName")
                    if not scene:
                        raise ObsError("OBS has no current scene")
                    shot = await self._request(reader, writer, "GetSourceScreenshot", {
                        "sourceName": scene, "imageFormat": fmt, "imageWidth": max_side, "imageHeight": max_side,
                        **({"imageCompressionQuality": 75} if fmt == "jpg" else {})})
                    image = str(shot.get("imageData") or "")
                    if "," not in image:
                        raise ObsError("OBS gave no picture")
                    return base64.b64decode(image.split(",", 1)[1], validate=False)
                finally:
                    writer.close()
        except ObsError:
            raise
        except TimeoutError as exc:
            raise ObsError("OBS did not answer in time") from exc
        except (OSError, asyncio.IncompleteReadError) as exc:
            raise ObsError("OBS closed the connection unexpectedly") from exc
