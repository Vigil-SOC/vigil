"""The evidence store lives in the State Directory, never the working directory (#2280).

The backend image runs as uid 1000 from a root-owned ``/app``; a store relative to
the working directory failed the first ``mkdir`` there, so every /hunt attach
answered 500 and every case page's evidence read did too.
"""

import os
import stat
from unittest.mock import MagicMock

import pytest

from core.cases.case_attachment_service import attach_document
from core.cases.case_evidence_service import CaseEvidenceService

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes through a read-only directory, so this proves nothing",
)


@pytest.fixture
def read_only_cwd(tmp_path, monkeypatch):
    """A non-root process whose working directory it cannot write, like ``/app``."""
    workdir = tmp_path / "app"
    workdir.mkdir()
    workdir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.chdir(workdir)
    state = tmp_path / "state"
    monkeypatch.setenv("VIGIL_DIR", str(state))
    yield workdir, state
    workdir.chmod(stat.S_IRWXU)


def test_an_attach_lands_in_the_state_directory(read_only_cwd, tmp_path):
    workdir, state = read_only_cwd
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF flash report")

    row = attach_document(
        MagicMock(),
        "CASE-1",
        source,
        "report.pdf",
        name=None,
        pages=2,
        collected_by="analyst",
    )

    stored = state / "evidence" / row.file_path
    assert stored.read_bytes() == b"%PDF flash report"
    assert row.file_size == len(b"%PDF flash report")
    assert list(workdir.iterdir()) == []


def test_reading_evidence_touches_no_disk(read_only_cwd, tmp_path, monkeypatch):
    # Even a State Directory that cannot be created leaves the read a query.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.setenv("VIGIL_DIR", str(blocked / "state"))
    session = MagicMock()
    query = session.query.return_value.filter.return_value
    query.order_by.return_value.all.return_value = []

    try:
        assert CaseEvidenceService().get_case_evidence("CASE-1", session=session) == []
    finally:
        blocked.chmod(stat.S_IRWXU)
    assert not (blocked / "state").exists()
