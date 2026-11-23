"""Turning raw screen pixels into a small PNG, in pure Python (no Pillow: unsigned native extensions are blocked on this PC).

The heavy lifting is done by C-level slicing and zlib, so a 1920x1080 screen takes about 10 ms.
"""
from __future__ import annotations

import array
import struct
import zlib

MAX_SIDE = 4096


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def rgb_to_png(width: int, height: int, rows: list[bytes]) -> bytes:
    """``rows`` are ``height`` byte strings of ``width * 3`` RGB bytes."""
    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 (none) in front of every row
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8 bit, colour type 2 (RGB)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 3)) + _chunk(b"IEND", b"")


def bgra_to_png(width: int, height: int, bgra: bytes, max_width: int = 960) -> bytes:
    """Windows screen pixels (4 bytes per pixel: blue, green, red, unused) -> a PNG no wider than about ``max_width``."""
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE) or len(bgra) < width * height * 4:
        raise ValueError("the pixel data does not match the size")
    step = max(1, -(-width // max(64, max_width)))  # keep every step-th pixel in both directions
    out_w, out_h = width // step, height // step
    if out_w < 1 or out_h < 1:
        raise ValueError("the image is too small")
    pixels = array.array("I")
    pixels.frombytes(bgra[: width * height * 4])
    rows = []
    for y in range(0, out_h * step, step):
        line = pixels[y * width:(y + 1) * width:step][:out_w].tobytes()
        rgb = bytearray(out_w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]  # swap blue and red, drop the unused byte
        rows.append(bytes(rgb))
    return rgb_to_png(out_w, out_h, rows)


def looks_blank(bgra: bytes) -> bool:
    """True if the capture is a single flat colour (a black frame is what a protected or hardware-drawn window gives)."""
    sample = bgra[::997]
    return len(set(sample)) <= 2
