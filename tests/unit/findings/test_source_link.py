"""Source links: an evidence ref, else a descriptor template."""

from core.findings.source_link import resolve_source_link
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
