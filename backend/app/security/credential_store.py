"""Encrypted-at-rest credential storage using Windows DPAPI, called through ``ctypes`` (system DLLs only, so
Smart App Control does not block it, and no third-party native code is involved).

DPAPI ties the ciphertext to the current Windows user account on this machine: another user, or the same file copied to
another PC, cannot decrypt it. OAuth tokens are therefore never stored in plaintext, in the database, in ``.env``, or in
the repository. (Anyone already running code *as this Windows user* can still ask DPAPI to decrypt them; that is inherent
to DPAPI and is the same protection browsers use for saved passwords.)
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_ENTROPY = b"stealthbot.credentials.v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


class CredentialError(Exception):
    """Credentials missing, unreadable, or not decryptable by this user."""


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _to_blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, max(len(data), 1))  # keep a reference alive for the call
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _from_blob(blob: _Blob) -> bytes:
        try:
            return ctypes.string_at(ctypes.cast(blob.pbData, ctypes.c_void_p), blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def protect(data: bytes, *, entropy: bytes = DEFAULT_ENTROPY) -> bytes:
        src, keep1 = _to_blob(data)
        ent, keep2 = _to_blob(entropy)
        out = _Blob()
        if not _crypt32.CryptProtectData(
            ctypes.byref(src), "StealthBot credentials", ctypes.byref(ent), None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        ):
            raise CredentialError(f"DPAPI encryption failed: {ctypes.WinError(ctypes.get_last_error())}")
        del keep1, keep2
        return _from_blob(out)

    def unprotect(blob: bytes, *, entropy: bytes = DEFAULT_ENTROPY) -> bytes:
        src, keep1 = _to_blob(blob)
        ent, keep2 = _to_blob(entropy)
        out = _Blob()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(src), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)
        ):
            raise CredentialError("Cannot decrypt the stored credentials (different Windows user or machine, or corrupt)")
        del keep1, keep2
        return _from_blob(out)

else:  # pragma: no cover - the project targets Windows 11
    def protect(data: bytes, *, entropy: bytes = DEFAULT_ENTROPY) -> bytes:
        raise CredentialError("DPAPI credential storage is only available on Windows")

    def unprotect(blob: bytes, *, entropy: bytes = DEFAULT_ENTROPY) -> bytes:
        raise CredentialError("DPAPI credential storage is only available on Windows")


class CredentialStore:
    """Named JSON documents, DPAPI-encrypted, one file per name (``<name>.dpapi``) inside ``directory``."""

    def __init__(self, directory: Path, *, entropy: bytes = DEFAULT_ENTROPY) -> None:
        self._dir = Path(directory)
        self._entropy = entropy

    def _path(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise ValueError(f"invalid credential name {name!r}")
        return self._dir / f"{name}.dpapi"

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def save(self, name: str, document: dict[str, Any]) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = protect(json.dumps(document).encode("utf-8"), entropy=self._entropy)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encrypted)
            os.replace(tmp, path)  # atomic: a crash never leaves a half-written credential file
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return json.loads(unprotect(path.read_bytes(), entropy=self._entropy).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise CredentialError(f"Stored credential {name!r} is corrupt") from exc

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False
