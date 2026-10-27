"""Unit tests for Microsoft Defender alert → finding transform."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.integrations._base.ids import FINDING_ID_MAX, fit_id
from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


@pytest.fixture
def sample_alerts():
    with open(FIXTURES_DIR / "defender_alerts.json") as f:
        return json.load(f)


@pytest.fixture
def ingestion():
    with patch(
        "core.integrations.microsoft_defender.ingestion.resolve",
        return_value={},
    ):
        svc = MicrosoftDefenderIngestion()
        svc.ingestion_service = MagicMock()
        yield svc


class TestTransformAlert:
    def test_emits_store_keys_from_title_and_evidence(self, ingestion, sample_alerts):
        finding = ingestion.transform_alert_to_finding(sample_alerts[0])
        assert finding is not None
        assert finding["finding_id"] == "defender-da637552173936382409"
        assert finding["external_id"] == "da637552173936382409"
        assert finding["data_source"] == "microsoft_defender"
        assert finding["severity"] == "high"
        # MDE left description empty; the text lives in title.
        assert finding["description"] == "Suspicious PowerShell command line"
        ctx = finding["entity_context"]
        assert "198.51.100.42" in ctx["src_ips"]
        assert "jsmith@corp.example" in ctx["usernames"]
        assert "WORKSTATION-01.corp.example" in ctx["hostnames"]
        assert (
            "aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
            in ctx["file_hashes"]
        )
        assert "ip_addresses" not in ctx
        assert ctx["category"] == "Execution"
        assert ctx["threat_family"] == "Emotet"
        assert finding["mitre_predictions"] == {"T1059.001": 1.0, "T1204.002": 1.0}

    def test_keeps_a_nonempty_description(self, ingestion, sample_alerts):
        alert = dict(sample_alerts[0])
        alert["description"] = "Encoded PowerShell spawned from a macro."
        finding = ingestion.transform_alert_to_finding(alert)
        assert finding["description"] == "Encoded PowerShell spawned from a macro."

    def test_handles_missing_fields_gracefully(self, ingestion):
        finding = ingestion.transform_alert_to_finding({"id": "sparse-1"})
        assert finding is not None
        assert finding["finding_id"] == "defender-sparse-1"
        assert finding["external_id"] == "sparse-1"
        assert finding["description"] == "Microsoft Defender Alert"
        assert finding["mitre_predictions"] == {}

    def test_handles_transform_error(self, ingestion):
        assert ingestion.transform_alert_to_finding(None) is None

    def test_42_char_alert_id_fits_and_keeps_raw_external_id(self, ingestion):
        # Repro: "defender-" + a 42-char MDE id is 51 and overflows String(50).
        alert_id = "ed1234abcd-12ab-4cde-9f01-23456789abcd_aml"
        finding = ingestion.transform_alert_to_finding({"id": alert_id})
        assert finding is not None
        assert len(alert_id) == 42
        assert len(finding["finding_id"]) <= 50
        assert finding["external_id"] == alert_id
        assert finding["finding_id"] == fit_id("defender-", alert_id, FINDING_ID_MAX)

    def test_long_ids_sharing_a_prefix_stay_distinct(self, ingestion):
        shared = "ed1234abcd-12ab-4cde-9f01-23456789abcd"
        first = ingestion.transform_alert_to_finding({"id": shared + "_aml"})
        second = ingestion.transform_alert_to_finding({"id": shared + "_xyz"})
        assert first["finding_id"] != second["finding_id"]
        assert len(first["finding_id"]) <= 50
        assert len(second["finding_id"]) <= 50
        assert first["external_id"] == shared + "_aml"
        assert second["external_id"] == shared + "_xyz"


class TestLegacyAlertShape:
    def test_top_level_fields_fill_entity_context(self, ingestion):
        finding = ingestion.transform_alert_to_finding(
            {
                "id": "legacy-1",
                "evidence": [],
                "computerDnsName": "host-1.corp.example",
                "loggedOnUsers": [
                    {"accountName": "jsmith", "domainName": "CORP"},
                    {"userName": "admin"},
                ],
                "domains": ["evil.example"],
            }
        )
        ctx = finding["entity_context"]
        assert ctx["hostnames"] == ["host-1.corp.example"]
        assert ctx["usernames"] == ["jsmith", "admin"]
        assert ctx["domains"] == ["evil.example"]

    def test_evidence_first_and_deduplicated(self, ingestion):
        finding = ingestion.transform_alert_to_finding(
            {
                "id": "legacy-2",
                "evidence": [
                    {"entityType": "Machine", "deviceDnsName": "host-1"},
                    {"entityType": "User", "userPrincipalName": "jsmith"},
                ],
                "computerDnsName": "host-1",
                "loggedOnUsers": [{"accountName": "other"}, {"userName": "jsmith"}],
            }
        )
        ctx = finding["entity_context"]
        assert ctx["hostnames"] == ["host-1"]
        assert ctx["usernames"] == ["jsmith", "other"]

    @pytest.mark.parametrize(
        "extra",
        [
            {"loggedOnUsers": "jsmith", "domains": "evil.example"},
            {"loggedOnUsers": [None, "x", 5, {"userName": None, "accountName": 7}]},
            {"domains": [None, 3, {"a": 1}, "ok.example"], "computerDnsName": 42},
        ],
    )
    def test_malformed_top_level_fields_do_not_drop_finding(self, ingestion, extra):
        finding = ingestion.transform_alert_to_finding({"id": "bad", **extra})
        assert finding is not None
        assert finding["entity_context"]["usernames"] == []
        assert finding["entity_context"]["hostnames"] == []
