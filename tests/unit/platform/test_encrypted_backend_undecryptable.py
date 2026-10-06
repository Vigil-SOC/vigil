"""An undecryptable secrets.enc must never be overwritten by a later save."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.secrets_manager import EncryptedFileBackend, _CRYPTOGRAPHY_AVAILABLE

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not _CRYPTOGRAPHY_AVAILABLE, reason="cryptography is not installed"),
]


@pytest.fixture
def stored(tmp_path):
    backend = EncryptedFileBackend(data_dir=tmp_path)
    assert backend.set("A", "1") and backend.set("B", "2")
    return backend, tmp_path


def _other_key() -> bytes:
    from cryptography.fernet import Fernet

    return Fernet.generate_key()


def test_wrong_master_key_refuses_writes_and_keeps_file(stored):
    _, tmp_path = stored
    original_key = (tmp_path / "master.key").read_bytes()
    original_blob = (tmp_path / "secrets.enc").read_bytes()
    (tmp_path / "master.key").write_bytes(_other_key())

    backend = EncryptedFileBackend(data_dir=tmp_path)
    assert backend.get("A") is None
    assert backend.set("C", "3") is False
    assert backend.delete("A") is True
    assert (tmp_path / "secrets.enc").read_bytes() == original_blob

    (tmp_path / "master.key").write_bytes(original_key)
    restored = EncryptedFileBackend(data_dir=tmp_path)
    assert restored.get("A") == "1"
    assert restored.get("B") == "2"


def test_restored_key_clears_failure_without_new_backend(stored):
    _, tmp_path = stored
    original_key = (tmp_path / "master.key").read_bytes()
    (tmp_path / "master.key").write_bytes(_other_key())

    backend = EncryptedFileBackend(data_dir=tmp_path)
    assert backend.set("C", "3") is False

    (tmp_path / "master.key").write_bytes(original_key)
    assert backend.get("A") == "1"
    assert backend.set("C", "3") is True
    assert backend.get("B") == "2"


def test_missing_master_key_refuses_writes_and_does_not_mint_one(stored):
    _, tmp_path = stored
    original_key = (tmp_path / "master.key").read_bytes()
    original_blob = (tmp_path / "secrets.enc").read_bytes()
    (tmp_path / "master.key").unlink()

    backend = EncryptedFileBackend(data_dir=tmp_path)
    assert backend.set("C", "3") is False
    assert not (tmp_path / "master.key").exists()
    assert (tmp_path / "secrets.enc").read_bytes() == original_blob

    (tmp_path / "master.key").write_bytes(original_key)
    assert backend.get("B") == "2"
    assert backend.set("C", "3") is True


def test_corrupt_file_refuses_writes(stored):
    _, tmp_path = stored
    (tmp_path / "secrets.enc").write_bytes(b"not a fernet token")

    backend = EncryptedFileBackend(data_dir=tmp_path)
    assert backend.set("C", "3") is False
    assert (tmp_path / "secrets.enc").read_bytes() == b"not a fernet token"
