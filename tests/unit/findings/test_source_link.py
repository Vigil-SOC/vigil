"""Source links: an evidence ref, else a descriptor template."""

from core.findings.source_link import descriptor_for_source, resolve_source_link
from core.integrations._base.descriptor import IntegrationDescriptor, IntegrationField

_DESCRIPTOR = IntegrationDescriptor(
    id="tr-link-src",
    category="SIEM",
    fields=(IntegrationField("server_url"), IntegrationField("token", secret=True)),
    console_link_template="https://{server_url}/alert/{external_id}",
)


def _descriptor(source: str):
    return _DESCRIPTOR if source == "tr-link-src" else None


def test_first_http_ref_wins_over_a_template(monkeypatch):
    monkeypatch.setattr("core.findings.source_link.get_descriptor", _descriptor)
    link = resolve_source_link(
        {
            "data_source": "tr-link-src",
            "external_id": "abc",
            "evidence_links": [
                {"ref": "notes"},
                {"ref": "ftp://files.example/a"},
                {"ref": "https://console.example/a"},
                {"ref": "https://console.example/b"},
            ],
        }
    )
    assert link == "https://console.example/a"


def test_template_uses_external_id_and_a_descriptor_field(monkeypatch):
    monkeypatch.setattr("core.findings.source_link.get_descriptor", _descriptor)
    monkeypatch.setattr(
        "core.integrations._base.config.get_integration_config",
        lambda _integration_id: {"server_url": "host.example", "token": "secret"},
    )
    link = resolve_source_link(
        {
            "data_source": "tr-link-src",
            "external_id": "abc",
            "evidence_links": [{"ref": "not a url"}],
        }
    )
    assert link == "https://host.example/alert/abc"


def test_missing_link_is_absent():
    assert (
        resolve_source_link({"data_source": "not-a-vendor", "evidence_links": []})
        is None
    )


def _config(monkeypatch, values):
    monkeypatch.setattr(
        "core.integrations._base.config.get_integration_config",
        lambda _integration_id: values,
    )


def test_stored_source_names_resolve_their_descriptors():
    for source, descriptor_id in {
        "elastic": "elastic-siem",
        "azure_sentinel": "azure-sentinel",
        "microsoft_defender": "microsoft-defender",
        "aws_security_hub": "aws-security-hub",
        "splunk": "splunk",
        "crowdstrike": "crowdstrike",
    }.items():
        assert descriptor_for_source(source).id == descriptor_id


def test_defender_fills_from_tenant_config_and_external_id(monkeypatch):
    _config(monkeypatch, {"tenant_id": "tenant-1", "client_secret": "s"})
    link = resolve_source_link(
        {"data_source": "microsoft_defender", "external_id": "da637578995287051192_1"}
    )
    assert link == (
        "https://security.microsoft.com/alerts/da637578995287051192_1?tid=tenant-1"
    )


def test_defender_without_tenant_id_has_no_link(monkeypatch):
    _config(monkeypatch, {"client_id": "c"})
    assert (
        resolve_source_link({"data_source": "microsoft_defender", "external_id": "a"})
        is None
    )


def test_evidence_ref_beats_the_defender_template(monkeypatch):
    _config(monkeypatch, {"tenant_id": "tenant-1"})
    link = resolve_source_link(
        {
            "data_source": "microsoft_defender",
            "external_id": "a",
            "evidence_links": [{"ref": "https://console.example/x"}],
        }
    )
    assert link == "https://console.example/x"


def test_sources_without_a_documented_url_have_no_link(monkeypatch):
    _config(monkeypatch, {"url": "https://splunk.example", "kibana_url": "https://k"})
    for source in ("splunk", "elastic", "crowdstrike", "azure_sentinel"):
        assert resolve_source_link({"data_source": source, "external_id": "a"}) is None
