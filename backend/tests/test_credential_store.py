import sys

import pytest

from app.security.credential_store import CredentialError, CredentialStore, protect, unprotect

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")

SECRET = {"access_token": "ya29.SUPER-SECRET-ACCESS", "refresh_token": "1//REFRESH-SECRET-VALUE", "n": 5}


def test_roundtrip(tmp_path):
    store = CredentialStore(tmp_path)
    assert store.load("youtube_tokens") is None and not store.exists("youtube_tokens")
    store.save("youtube_tokens", SECRET)
    assert store.exists("youtube_tokens") and store.load("youtube_tokens") == SECRET
    assert store.delete("youtube_tokens") and not store.delete("youtube_tokens")


def test_nothing_is_stored_in_plaintext(tmp_path):
    CredentialStore(tmp_path).save("youtube_tokens", SECRET)
    raw = (tmp_path / "youtube_tokens.dpapi").read_bytes()
    assert b"SUPER-SECRET" not in raw and b"REFRESH-SECRET" not in raw and b"access_token" not in raw
    assert all(b"SECRET" not in f.read_bytes() for f in tmp_path.iterdir())


def test_wrong_entropy_cannot_decrypt(tmp_path):
    CredentialStore(tmp_path, entropy=b"A").save("x", SECRET)
    with pytest.raises(CredentialError):
        CredentialStore(tmp_path, entropy=b"B").load("x")


def test_corrupt_files_raise_a_clear_error(tmp_path):
    (tmp_path / "x.dpapi").write_bytes(b"not a dpapi blob")
    with pytest.raises(CredentialError):
        CredentialStore(tmp_path).load("x")


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    store = CredentialStore(tmp_path)
    store.save("x", {"v": 1})
    store.save("x", {"v": 2})
    assert store.load("x") == {"v": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.dpapi"]


def test_names_are_validated_against_path_tricks(tmp_path):
    store = CredentialStore(tmp_path)
    for bad in ("../evil", "a/b", "A", "", "x" * 100):
        with pytest.raises(ValueError):
            store.save(bad, {})


def test_low_level_roundtrip_including_empty_and_binary():
    for data in (b"", b"\x00\x01\xff" * 1000, "ünïcode".encode()):
        assert unprotect(protect(data)) == data
