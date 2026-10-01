"""A failed encrypted write must not leave the unsaved value in the cache."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.secrets_manager import EncryptedFileBackend, _CRYPTOGRAPHY_AVAILABLE

pytestmark = pytest.mark.unit


@pytest.mark.skipif(not _CRYPTOGRAPHY_AVAILABLE, reason="cryptography is not installed")
def test_failed_set_keeps_previous_value(tmp_path):
    backend = EncryptedFileBackend(data_dir=tmp_path)
    assert backend.set("SPLUNK_PASSWORD", "old-secret") is True

    with patch.object(backend, "_write_cache", return_value=False):
        assert backend.set("SPLUNK_PASSWORD", "new-secret") is False

    assert backend.get("SPLUNK_PASSWORD") == "old-secret"
    backend._cache = None
    assert backend.get("SPLUNK_PASSWORD") == "old-secret"


@pytest.mark.skipif(not _CRYPTOGRAPHY_AVAILABLE, reason="cryptography is not installed")
def test_failed_set_drops_key_that_was_never_stored(tmp_path):
    backend = EncryptedFileBackend(data_dir=tmp_path)

    with patch.object(backend, "_write_cache", return_value=False):
        assert backend.set("SPLUNK_PASSWORD", "new-secret") is False

    assert backend.get("SPLUNK_PASSWORD") is None
    backend._cache = None
    assert backend.get("SPLUNK_PASSWORD") is None
