"""Evidence ``file_path`` comes from the request body and must stay in the store."""

import pytest

from core.cases.case_evidence_service import CaseEvidenceService

pytestmark = pytest.mark.unit


@pytest.fixture
def service(tmp_path):
    store = tmp_path / "evidence"
    (store / "case-1").mkdir(parents=True)
    (store / "case-1" / "pcap.bin").write_bytes(b"pcap")
    (tmp_path / "secret.txt").write_text("not evidence")
    return CaseEvidenceService(storage_path=str(store))


def test_a_file_in_the_store_resolves(service):
    path = service._stored_file("case-1/pcap.bin")
    assert path is not None and path.read_bytes() == b"pcap"


@pytest.mark.parametrize(
    "escape", ["../secret.txt", "case-1/../../secret.txt", "/etc/passwd"]
)
def test_a_path_out_of_the_store_is_refused(service, escape):
    assert service._stored_file(escape) is None
