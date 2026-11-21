"""RSA PKCS#1 v1.5 + SHA-256 signature VERIFICATION in pure Python.

Kick signs every webhook with its private key; the bot must check the signature with Kick's public key, because the webhook
listener is reachable from the internet. The usual library (``cryptography``) is a native extension that Windows Smart App
Control may block on this PC, and verification needs only one modular exponentiation with the PUBLIC exponent, so it is done
here. Only verification exists in this module: there is no signing and no key generation (tests bring their own).

Rules followed (RFC 8017 section 8.2.2): the signature must be exactly as long as the modulus and smaller than it, and the
decrypted block must equal the expected padded block bit for bit (compared in constant time). The block is rebuilt and
compared, not parsed, so malformed padding (the classic source of signature-forgery bugs) cannot be accepted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re

# DER encoding of DigestInfo for SHA-256 (RFC 8017 section 9.2, note 1), followed in the signature block by the 32-byte hash
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")
MIN_MODULUS_BITS = 1024
MAX_MODULUS_BITS = 8192


class KeyError_(ValueError):
    """The public key could not be understood (named with a trailing underscore to avoid shadowing KeyError)."""


# ---------------------------------------------------------------------------- tiny strict DER reader
def _read(data: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """Read one DER element header at ``pos``: returns (tag, content_start, content_end). Strict about bounds."""
    if pos + 2 > end:
        raise KeyError_("truncated DER")
    tag = data[pos]
    first = data[pos + 1]
    pos += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 3 or pos + count > end:  # indefinite or absurd lengths are not valid DER for a key
            raise KeyError_("bad DER length")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > end:
        raise KeyError_("DER element runs past its parent")
    return tag, pos, pos + length


def _integer(data: bytes, start: int, end: int) -> int:
    tag, s, e = _read(data, start, end)
    if tag != 0x02 or e != end or e == s:
        raise KeyError_("expected an INTEGER")
    value = int.from_bytes(data[s:e], "big")
    if data[s] & 0x80:
        raise KeyError_("negative INTEGER")
    return value


def _pkcs1_key(data: bytes, start: int, end: int) -> tuple[int, int]:
    """SEQUENCE { INTEGER modulus, INTEGER publicExponent } occupying exactly [start, end)."""
    tag, s, e = _read(data, start, end)
    if tag != 0x30 or e != end:
        raise KeyError_("expected a SEQUENCE")
    tag, ns, ne = _read(data, s, e)
    if tag != 0x02:
        raise KeyError_("expected the modulus")
    tag2, es, ee = _read(data, ne, e)
    if tag2 != 0x02 or ee != e:
        raise KeyError_("expected the public exponent")
    n = int.from_bytes(data[ns:ne], "big")
    exponent = int.from_bytes(data[es:ee], "big")
    return n, exponent


def parse_public_key(pem: str) -> tuple[int, int]:
    """Parse a PEM 'PUBLIC KEY' (SubjectPublicKeyInfo) or 'RSA PUBLIC KEY' (PKCS#1) into (modulus, exponent)."""
    match = re.search(r"-----BEGIN (RSA )?PUBLIC KEY-----(.+?)-----END (RSA )?PUBLIC KEY-----", pem or "", re.S)
    if not match:
        raise KeyError_("no PEM public key found")
    try:
        der = base64.b64decode("".join(match.group(2).split()), validate=True)
    except ValueError as exc:
        raise KeyError_("the key is not valid base64") from exc
    if match.group(1):  # "RSA PUBLIC KEY": the bare PKCS#1 structure
        n, e = _pkcs1_key(der, 0, len(der))
    else:  # SubjectPublicKeyInfo { AlgorithmIdentifier, BIT STRING { PKCS#1 key } }
        tag, s, end = _read(der, 0, len(der))
        if tag != 0x30 or end != len(der):
            raise KeyError_("expected a SEQUENCE")
        tag, as_, ae = _read(der, s, end)
        if tag != 0x30:
            raise KeyError_("expected the algorithm")
        oid_tag, os_, oe = _read(der, as_, ae)
        if oid_tag != 0x06 or der[os_:oe] != _RSA_ENCRYPTION_OID:
            raise KeyError_("not an RSA key")
        tag, bs, be = _read(der, ae, end)
        if tag != 0x03 or be != end or der[bs] != 0:
            raise KeyError_("expected the key bits")
        n, e = _pkcs1_key(der, bs + 1, be)
    if not MIN_MODULUS_BITS <= n.bit_length() <= MAX_MODULUS_BITS:
        raise KeyError_(f"the modulus must be {MIN_MODULUS_BITS}-{MAX_MODULUS_BITS} bits")
    if e < 3 or e % 2 == 0 or e.bit_length() > 64:
        raise KeyError_("unusual public exponent")
    return n, e


# ---------------------------------------------------------------------------- verification
def verify_pkcs1_v15_sha256(public_key: tuple[int, int], message: bytes, signature: bytes) -> bool:
    n, e = public_key
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    block = pow(s, e, n).to_bytes(k, "big")
    tail = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    if k < len(tail) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(tail) - 3) + b"\x00" + tail
    return hmac.compare_digest(block, expected)
