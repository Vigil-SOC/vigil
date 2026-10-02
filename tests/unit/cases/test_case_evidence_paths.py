"""Evidence file paths stay inside the evidence store."""

import hashlib
import os
from unittest.mock import MagicMock

import pytest

from core.cases.case_evidence_service import CaseEvidenceService


@pytest.fixture
def service(tmp_path):
    return CaseEvidenceService(storage_path=str(tmp_path / "evidence"))


def test_hashes_a_file_inside_the_store(service):
    (service.storage_path / "dump.raw").write_bytes(b"memory")

    evidence = service.add_evidence(
        "case-1", "file", "dump", "analyst", file_path="dump.raw", session=MagicMock()
    )

    assert evidence.file_hash_sha256 == hashlib.sha256(b"memory").hexdigest()
    assert evidence.file_size == 6


@pytest.mark.parametrize(
    "escape", ["../secret.txt", "/etc/passwd", "a/../../secret.txt"]
)
def test_refuses_paths_outside_the_store(service, escape):
    (service.storage_path.parent / "secret.txt").write_text("x")
    session = MagicMock()

    with pytest.raises(ValueError):
        service.add_evidence(
            "case-1", "file", "x", "analyst", file_path=escape, session=session
        )
    session.add.assert_not_called()


def test_refuses_symlink_out_of_the_store(service):
    (service.storage_path.parent / "secret.txt").write_text("x")
    os.symlink(
        service.storage_path.parent / "secret.txt", service.storage_path / "link"
    )

    with pytest.raises(ValueError):
        service.resolve_stored_file("link")
