"""Tolerant decoder for an HTTP response that carries a *stream* of JSON documents.

EXPERIMENTAL. Google documents ``liveChatMessages.streamList`` for gRPC only; the discovery document lists a plain
HTTP path (GET /youtube/v3/liveChat/messages/stream) but nothing documents its framing. Server-streaming REST
endpoints at Google commonly return one JSON array delivered in chunks (``[{...},\\n{...},...``) but newline-delimited
JSON is also plausible, so this decoder accepts both, plus concatenated objects. It is exercised with synthetic data in
the tests; the real framing must be confirmed with ``scripts\\youtube_check.py --probe-stream`` (see docs).
"""
from __future__ import annotations

import json
from typing import Any

_SKIP = " \t\r\n,["
_MAX_BUFFER = 8 * 1024 * 1024  # a single JSON document larger than this is treated as a broken stream


class StreamFramingError(Exception):
    pass


class JsonStreamDecoder:
    def __init__(self) -> None:
        self._buf = ""
        self._decoder = json.JSONDecoder()

    def finish(self) -> None:
        """Call when the server closed the stream: leftover text means it ended in the middle of a document."""
        rest = self._buf.lstrip(_SKIP).lstrip("]").strip()
        if rest:
            raise StreamFramingError("stream ended in the middle of a document")

    def feed(self, text: str) -> list[Any]:
        """Add a chunk of text; return every JSON document that is now complete."""
        self._buf += text
        if len(self._buf) > _MAX_BUFFER:
            raise StreamFramingError("stream buffer exceeded the size limit without a complete document")
        out: list[Any] = []
        while True:
            self._buf = self._buf.lstrip(_SKIP)
            if self._buf.startswith("]"):  # end of a streamed array
                self._buf = self._buf[1:]
                continue
            if not self._buf:
                return out
            try:
                obj, end = self._decoder.raw_decode(self._buf)
            except json.JSONDecodeError:
                if self._buf[0] not in '{"[0123456789-tfn':
                    raise StreamFramingError(f"unexpected data in stream (starts with {self._buf[0]!r})") from None
                return out  # an incomplete document: wait for more bytes
            out.append(obj)
            self._buf = self._buf[end:]
