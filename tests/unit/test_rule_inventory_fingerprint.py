"""What makes a new rule version, and what does not."""

from core.detections.inventory.readers import splunk_record
from core.detections.inventory.record import RuleRecord, fingerprint, techniques_in


def _spl(query, **kw):
    return RuleRecord(
        native_key="search/x", name="x", query=query, language="spl", **kw
    )


def test_whitespace_and_spl_comments_are_not_a_change():
    base = _spl("index=main | stats count by host")
    reflowed = _spl("```note```  index=main\n|   stats count  by host ")
    assert fingerprint(base) == fingerprint(reflowed)


def test_name_description_and_enabled_are_not_a_change():
    a = _spl("index=main", raw={"description": "one"})
    b = RuleRecord(
        native_key="search/x",
        name="renamed",
        query="index=main",
        language="spl",
        enabled=False,
        raw={"description": "two"},
    )
    assert fingerprint(a) == fingerprint(b)


def test_query_and_logic_changes_are():
    base = _spl("index=main", logic={"alert.severity": "3"})
    assert fingerprint(base) != fingerprint(_spl("index=other", logic=base.logic))
    assert fingerprint(base) != fingerprint(
        _spl("index=main", logic={"alert.severity": "5"})
    )


def test_splunk_lists_only_scheduled_or_correlation_searches():
    adhoc = {"name": "a", "acl": {"app": "search"}, "content": {"search": "x"}}
    scheduled = {
        "name": "b",
        "acl": {"app": "search"},
        "content": {"search": "x", "is_scheduled": True, "disabled": True},
    }
    assert splunk_record(adhoc) is None
    record = splunk_record(scheduled)
    assert record.native_key == "search/b" and record.enabled is False


def test_techniques_are_upper_cased_and_unique():
    assert techniques_in([["attack.t1059.001", "T1059.001"], {"id": "T1110"}]) == [
        "T1059.001",
        "T1110",
    ]
