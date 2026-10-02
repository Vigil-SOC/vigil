"""GET /cases/{id} lists linked findings with the Overview source resolver."""

from unittest.mock import MagicMock

import pytest

from core.integrations._base.descriptor import IntegrationDescriptor, IntegrationField
from core.storage.schemas.case_api import CaseDetailResponse

pytestmark = pytest.mark.unit

_DESCRIPTOR = IntegrationDescriptor(
    id="tr-link-src",
    category="SIEM",
    fields=(IntegrationField("server_url"),),
    console_link_template="https://{server_url}/alert/{external_id}",
)


class _Row:
    def __init__(self, finding_id, description, **kw):
        self.finding_id = finding_id
        self.description = description
        self.evidence_links = kw.get("evidence_links")
        self.data_source = kw.get("data_source")
        self.external_id = kw.get("external_id")


def _descriptor(source: str):
    return _DESCRIPTOR if source == "tr-link-src" else None


@pytest.mark.asyncio
async def test_get_case_links_rows_that_exist_and_skips_a_missing_id(monkeypatch):
    from core.api.v1 import cases_router as cases

    rows = [
        _Row(
            "f-http",
            "console alert",
            evidence_links=[{"ref": "note"}, {"ref": "https://console.example/a"}],
            data_source="tr-link-src",
            external_id="http-1",
        ),
        _Row(
            "f-tpl",
            "template alert",
            evidence_links=[{"ref": "not a url"}],
            data_source="tr-link-src",
            external_id="abc",
        ),
        _Row(
            "f-tpl-2",
            "same source",
            evidence_links=[],
            data_source="tr-link-src",
            external_id="def",
        ),
        _Row(
            "f-empty",
            "no door",
            evidence_links=[{"ref": "just text"}],
            data_source="not-a-vendor",
            external_id=None,
        ),
    ]
    seen = {"calls": 0, "ids": None}

    def resolve_findings(self, finding_ids):
        seen["calls"] += 1
        seen["ids"] = list(finding_ids)
        return rows

    config_calls = {"n": 0}

    def config(_integration_id):
        config_calls["n"] += 1
        return {"server_url": "host.example"}

    monkeypatch.setattr(cases.CaseRepository, "resolve_findings", resolve_findings)
    monkeypatch.setattr("core.findings.source_link.get_descriptor", _descriptor)
    monkeypatch.setattr(
        "core.integrations._base.config.get_integration_config", config
    )
    monkeypatch.setattr(
        cases.data_service,
        "get_case",
        lambda case_id: {
            "case_id": "c1",
            "title": "Linked",
            "status": "open",
            "finding_ids": ["f-http", "missing", "f-tpl", "f-tpl-2", "f-empty"],
        },
    )
    monkeypatch.setattr(
        cases.case_records_service,
        "list_case_investigations",
        lambda session, case_id: [],
    )
    session = MagicMock()
    session.get.return_value = None

    result = await cases.get_case("c1", session)
    parsed = CaseDetailResponse.model_validate(result).model_dump()

    assert parsed["finding_ids"] == ["f-http", "missing", "f-tpl", "f-tpl-2", "f-empty"]
    assert "findings" not in parsed
    assert seen["calls"] == 1
    assert seen["ids"] == parsed["finding_ids"]
    assert config_calls["n"] == 1
    by_id = {item["finding_id"]: item for item in parsed["linked_findings"]}
    assert list(by_id) == ["f-http", "f-tpl", "f-tpl-2", "f-empty"]
    assert by_id["f-http"]["source_link"] == "https://console.example/a"
    assert by_id["f-http"]["description"] == "console alert"
    assert by_id["f-tpl"]["source_link"] == "https://host.example/alert/abc"
    assert by_id["f-tpl-2"]["source_link"] == "https://host.example/alert/def"
    assert by_id["f-empty"]["description"] == "no door"
    assert by_id["f-empty"]["source_link"] is None
