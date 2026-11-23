"""Taking a picture of what the stream shows, from this PC's screen (Windows GDI through ctypes; nothing to install).

``whole screen`` captures the primary monitor, which also shows anything else you have open: when a window name is given only that
window is captured (``PrintWindow``), so a private window next to your game is never in the picture. If that window cannot be
captured (some hardware-drawn full-screen games give a black frame) the answer is an error, never a fallback to the whole screen.
Prefer OBS (``obs.py``): it gives exactly what viewers see.
"""
from __future__ import annotations

import sys

from .imaging import bgra_to_png, looks_blank


class CaptureError(Exception):
    """A picture could not be taken; the message is safe to show in the panel."""


def _win32():  # noqa: ANN202
    if sys.platform != "win32":
        raise CaptureError("screen capture only works on Windows")
    import ctypes
    from ctypes import wintypes

    return ctypes, wintypes


def list_window_titles() -> list[str]:
    """Titles of the visible top-level windows (for the local dashboard only: titles can be private, so they are never published)."""
    ctypes, wintypes = _win32()
    user32 = ctypes.windll.user32
    titles: list[str] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _lparam):  # noqa: ANN001, ANN202
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(proc(each), 0)
    return titles


def _find_window(needle: str):  # noqa: ANN202
    ctypes, wintypes = _win32()
    user32 = ctypes.windll.user32
    found: list[int] = []
    wanted = needle.casefold()
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _lparam):  # noqa: ANN001, ANN202
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if wanted in buf.value.casefold():
                    found.append(hwnd)
        return True

    user32.EnumWindows(proc(each), 0)
    return found[0] if found else None


def capture_png(window: str = "", max_width: int = 960) -> bytes:
    """PNG of the primary screen, or of the first visible window whose title contains ``window``."""
    ctypes, wintypes = _win32()
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels, not scaled ones
    except Exception:  # noqa: BLE001 - older Windows
        user32.SetProcessDPIAware()
    hwnd = None
    if window.strip():
        hwnd = _find_window(window.strip())
        if hwnd is None:
            raise CaptureError(f"no open window has '{window.strip()[:40]}' in its title")
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width, height = rect.right - rect.left, rect.bottom - rect.top
    else:
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if width < 64 or height < 64:
        raise CaptureError("the window is too small to look at")

    screen_dc = user32.GetDC(0)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    try:
        gdi32.SelectObject(memory_dc, bitmap)
        if hwnd is not None:
            if not user32.PrintWindow(hwnd, memory_dc, 2):  # 2 = PW_RENDERFULLCONTENT (also windows drawn by the GPU)
                raise CaptureError("Windows would not give a picture of that window")
        elif not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, 0, 0, 0x00CC0020 | 0x40000000):  # SRCCOPY | CAPTUREBLT
            raise CaptureError("Windows would not give a picture of the screen")

        class Header(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG), ("planes", wintypes.WORD),
                        ("bits", wintypes.WORD), ("compression", wintypes.DWORD), ("image", wintypes.DWORD),
                        ("xppm", wintypes.LONG), ("yppm", wintypes.LONG), ("used", wintypes.DWORD), ("important", wintypes.DWORD)]

        info = Header(ctypes.sizeof(Header), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)  # a negative height means top-down rows
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0):
            raise CaptureError("could not read the picture's pixels")
        pixels = buffer.raw
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)
    if looks_blank(pixels):
        raise CaptureError("the picture is blank (a hardware-drawn full-screen window gives a black frame): use OBS instead")
    return bgra_to_png(width, height, pixels, max_width)
