"""Analyst IP exclusions without a database: which addresses a finding names and
which of them are excluded (core.findings.exclusions)."""

import pytest

from core.findings import exclusions as ex
from core.findings.ip_address import normalize_ip

pytestmark = pytest.mark.unit

SCANNER = "203.0.113.9"


def _finding(fid="f-1", **entity_context):
    return {"finding_id": fid, "entity_context": entity_context}


# --- one address, one spelling ----------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("203.0.113.9", "203.0.113.9"),
        ("  203.0.113.9\n", "203.0.113.9"),
        ("2001:DB8:0:0::1", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
    ],
)
def test_normalize_ip_accepts_one_address(raw, expected):
    assert normalize_ip(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "not-an-ip", "10.0.0.0/8", "10.0.0.1:443", "999.1.1.1", None, 42]
)
def test_normalize_ip_refuses_anything_else(raw):
    assert normalize_ip(raw) is None


# --- which addresses a finding names ------------------------------------------


def test_finding_ips_reads_scalars_and_lists_across_source_spellings():
    context = {
        "src_ip": "10.0.0.5",
        "dst_ips": ["203.0.113.9", "bogus"],
        "dest_ips": ["198.51.100.7"],
        "destination_ip": "2001:DB8::1",
        "hostnames": ["web-1"],
    }
    assert ex.finding_ips(context) == {
        "10.0.0.5",
        "203.0.113.9",
        "198.51.100.7",
        "2001:db8::1",
    }


def test_finding_ips_ignores_source_evidence_records():
    """A netflow preview names every peer in its window; it is not the finding."""
    context = {
        "src_ip": "10.0.0.5",
        "source_evidence": {"records": [{"src_ip": SCANNER, "dst_ip": SCANNER}]},
    }
    assert ex.finding_ips(context) == {"10.0.0.5"}


@pytest.mark.parametrize("context", [None, "text", [], {}])
def test_finding_ips_is_empty_without_entity_context(context):
    assert ex.finding_ips(context) == frozenset()


# --- which findings an exclusion hides ------------------------------------------


def test_any_excluded_address_hides_the_finding():
    """The scanner's findings all name one of our hosts too; hiding needs "any"."""
    finding = _finding(src_ip=SCANNER, dst_ip="10.0.0.5")
    assert ex.excluded_ips_of(finding, {SCANNER}) == [SCANNER]
    assert ex.excluded_ips_of(_finding(src_ip="10.0.0.6"), {SCANNER}) == []


def test_the_cache_answers_without_a_database(active_exclusions):
    assert ex.cached_active_ips() == {SCANNER}
    active_exclusions.set(set())
    assert ex.cached_active_ips() == frozenset()


def test_cached_active_ips_fails_open_when_the_database_is_unreadable(monkeypatch):
    """Failing closed would hide the whole queue."""
    import core.storage.unit_of_work as uow

    def boom():
        raise RuntimeError("postgres is down")

    ex.invalidate_cache()
    monkeypatch.setattr(uow, "unit_of_work", boom)
    assert ex.cached_active_ips() == frozenset()
    ex.invalidate_cache()
