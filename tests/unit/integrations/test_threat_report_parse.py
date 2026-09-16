"""`parse_report`: a STIX bundle or a pasted text report -> Entity Keys + T-IDs.

Sits beside the `parse_stix_indicator` tests in test_cloudflare_integration.py.
"""

from __future__ import annotations

import json

import pytest

from core.memory.entity_keys import text_entity_keys
from core.threat_intel.threat_feed_service import parse_report

_BUNDLE_OBJECTS = [
    {
        "type": "indicator",
        "id": "indicator--1",
        "pattern": "[ipv4-addr:value = '1.2.3.4']",
        "pattern_type": "stix",
    },
    {
        "type": "attack-pattern",
        "id": "attack-pattern--1",
        "name": "Web Protocols",
        "external_references": [
            {"source_name": "mitre-attack", "external_id": "T1071.001"},
            {"source_name": "capec", "external_id": "CAPEC-1"},
        ],
    },
]


@pytest.mark.parametrize(
    "bundle",
    [
        _BUNDLE_OBJECTS,
        {"type": "bundle", "id": "bundle--1", "objects": _BUNDLE_OBJECTS},
    ],
    ids=["list", "objects"],
)
def test_stix_bundle_yields_entity_key_and_technique(bundle):
    assert parse_report(json.dumps(bundle)) == {
        "entity_keys": ["ip:1.2.3.4"],
        "techniques": ["T1071.001"],
    }


def test_stix_hash_indicator_mints_hash_not_hash_md5():
    obj = {
        "type": "indicator",
        "pattern": "[file:hashes.MD5 = 'D41D8CD98F00B204E9800998ECF8427E' "
        "OR domain-name:value = 'Evil[.]com']",
    }
    assert parse_report(json.dumps([obj]))["entity_keys"] == [
        "hash:d41d8cd98f00b204e9800998ecf8427e",
        "domain:evil.com",
    ]


def test_plain_text_defanged_ip_and_technique():
    out = parse_report(
        "C2 at 1[.]2[.]3[.]4 and evil[.]com via hxxp://evil[.]com/x, "
        "matching T1071.001 and T1059; T1059 again."
    )
    assert out["entity_keys"] == [
        "url:http://evil.com/x",
        "ip:1.2.3.4",
        "domain:evil.com",
    ]
    assert out["techniques"] == ["T1071.001", "T1059"]


def test_plain_text_follows_hunt_extractor_well_formed_rules():
    assert text_entity_keys("upgraded to version 1.2.3.4 today") == []
    assert text_entity_keys("dropped payload.exe and backup.zip") == []
    assert text_entity_keys("octet 999.1.1.1 is no address") == []
    assert text_entity_keys("host evil.com.") == ["domain:evil.com"]


@pytest.mark.parametrize("text", ["", "   ", "{not json", '{"foo": 1}', "[]", "42"])
def test_empty_or_unrecognised_input_gives_empty_lists(text):
    assert parse_report(text) == {"entity_keys": [], "techniques": []}


def test_unrecognised_json_falls_through_to_text():
    assert parse_report('{"note": "seen 8.8.8.8 with T1566"}') == {
        "entity_keys": ["ip:8.8.8.8"],
        "techniques": ["T1566"],
    }
