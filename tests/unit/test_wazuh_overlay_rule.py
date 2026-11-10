"""A rule can be written in Wazuh's format: saved as an overlay copy (one
<rule> in its own .xml file), versioned like the others, its techniques read
from <mitre>, linted on site values, and its id in Wazuh's custom range and
clear of the ids the Wazuh manager has loaded."""

import asyncio
from pathlib import Path

import pytest

from core.detections import overlay, wazuh_xml
from core.detections.inventory import readers
from core.detections.lint import lint_record, lint_wazuh
from core.detections.tools import SecurityDetectionsTools

RULE = """<group name="vigil,windows,">
  <rule id="100100" level="12">
    <if_sid>61603</if_sid>
    <field name="win.eventdata.image" type="pcre2">(?i)\\\\rundll32\\.exe$</field>
    <field name="win.eventdata.commandLine" type="pcre2">(?i)javascript:</field>
    <description>Rundll32 proxying JavaScript</description>
    <mitre><id>T1218.011</id><id>t1059</id></mitre>
  </rule>
</group>
"""


def _rule(body, attrs='id="100100" level="5"'):
    return f"<rule {attrs}>{body}<description>x</description></rule>"


# --- reading -----------------------------------------------------------------


def test_a_wazuh_file_reads_as_one_rule_keyed_by_its_id():
    record = readers.parse_file("auto", Path("r.xml"), "wazuh/r.xml", RULE)
    assert record.language == "wazuh" and record.native_key == "100100"
    assert record.name == "Rundll32 proxying JavaScript"
    assert record.techniques == ["T1218.011", "T1059"]
    assert record.logic["level"] == 12 and record.logic["if_sid"] == "61603"
    assert record.logic["groups"] == ["vigil", "windows"]
    assert "<if_sid>61603</if_sid>" in record.query
    assert record.content["_content"] == RULE


def test_the_overlay_and_auto_sources_read_xml_files():
    assert ".xml" in readers.FORMAT_SUFFIXES["auto"]
    assert readers.FORMAT_SUFFIXES["wazuh"] == (".xml",)


@pytest.mark.parametrize(
    "text",
    [
        RULE + RULE,  # a ruleset file of many rules
        "<group name='x'></group>",  # no rule
        _rule("<match>x</match>", attrs='level="5"'),  # no id
        '<!DOCTYPE r [<!ENTITY a "b">]><rule id="100100"/>',
        "<rule id='100100'>",  # not well-formed
    ],
    ids=["many", "none", "no-id", "doctype", "broken"],
)
def test_anything_but_one_rule_with_an_id_is_no_rule(text):
    assert readers.parse_file("auto", Path("r.xml"), "r.xml", text) is None


# --- lint ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, kind, value",
    [
        (
            '<field name="win.eventdata.destinationIp">10.20.30.40</field>',
            "ip",
            "10.20.30.40",
        ),
        ("<regex>^10\\.20\\.30\\.40$</regex>", "ip", "10.20.30.40"),
        ("<match>fs01.acme.corp</match>", "hostname", "fs01.acme.corp"),
        ("<hostname>dc01</hostname>", "hostname", "dc01"),
        ("<system_name>WS-FIN-07</system_name>", "hostname", "WS-FIN-07"),
        ('<field name="win.eventdata.targetUserName">jsmith</field>', "user", "jsmith"),
        ("<srcip>!192.168.10.5</srcip>", "ip", "192.168.10.5"),
    ],
    ids=[
        "field-ip",
        "regex-ip",
        "match-fqdn",
        "hostname",
        "system-name",
        "user",
        "not-ip",
    ],
)
def test_lint_fails_a_wazuh_rule_on_a_site_value(body, kind, value):
    report = lint_wazuh(_rule(body))
    assert report["passed"] is False
    assert (kind, value) in [(f["kind"], f["value"]) for f in report["findings"]]


@pytest.mark.parametrize(
    "body",
    [
        "<srcip>10.0.0.0/8</srcip>",
        '<field name="win.eventdata.image">\\\\rundll32\\.exe$</field>',
        "<match>Failed password for</match>",
    ],
    ids=["private-range", "image", "message"],
)
def test_lint_passes_a_wazuh_rule_on_behaviour(body):
    assert lint_wazuh(_rule(body))["passed"] is True


def test_lint_passes_a_wazuh_rule_on_a_vendor_built_in_account():
    # Wazuh's stock rule 7420 (0125-symantec-ws_rules.xml): Symantec Web
    # Security's appliance admin logging in (#4105).
    body = "<if_sid>7415</if_sid><user>virtadmin</user>"
    assert lint_wazuh(_rule(body, 'id="7420" level="3"'))["passed"] is True


def test_a_host_the_rule_only_leaves_out_is_a_warning():
    report = lint_wazuh(_rule('<hostname negate="yes">dc01</hostname><match>x</match>'))
    assert report["passed"] is True
    assert [(w["kind"], w["value"]) for w in report["warnings"]] == [
        ("hostname", "dc01")
    ]


def test_lint_record_lints_a_wazuh_copy():
    bad = RULE.replace(
        "(?i)javascript:</field>",
        '(?i)javascript:</field><field name="data.dstip">10.20.30.40</field>',
    )
    record = readers.parse_file("auto", Path("r.xml"), "r.xml", bad)
    assert lint_record(record)["passed"] is False


# --- the id --------------------------------------------------------------------


def test_with_id_sets_the_id_and_keeps_the_rest():
    out = wazuh_xml.with_id('<rule level="5" id="7" frequency="2">', 100123)
    assert out == '<rule id="100123" level="5" frequency="2">'


@pytest.fixture
def ids(monkeypatch):
    """The ids the manager loaded and the overlay's Wazuh copies hold."""
    box = {"loaded": set(), "own": set()}
    monkeypatch.setattr(
        overlay, "_wazuh_ids", lambda session: (box["loaded"], box["own"])
    )

    class _Scope:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        overlay,
        "get_db_manager",
        lambda: type("M", (), {"session_scope": lambda s: _Scope()})(),
    )
    return box


def test_a_copy_with_no_id_gets_the_lowest_free_one(ids):
    ids["loaded"] = {"100000", "100001", "5710"}
    ids["own"] = {"100002"}
    out = overlay._wazuh_with_id(_rule("<match>x</match>", attrs='level="5"'))
    assert wazuh_xml.rule_id(wazuh_xml.parse(out)) == "100003"


def test_an_id_the_manager_loaded_is_refused(ids):
    ids["loaded"] = {"100100"}
    with pytest.raises(overlay.Refused, match="already loaded"):
        overlay._wazuh_with_id(RULE)


def test_an_overlay_copys_own_id_is_kept_once_the_manager_loads_it(ids):
    # A deployed rule is loaded under the id it carries: changing it is allowed.
    ids["loaded"] = ids["own"] = {"100100"}
    assert overlay._wazuh_with_id(RULE) == RULE


@pytest.mark.parametrize("given", ["5710", "120001", "abc"])
def test_an_id_outside_the_custom_range_is_refused(ids, given):
    with pytest.raises(overlay.Refused, match="outside Wazuh's range"):
        overlay._wazuh_with_id(RULE.replace('id="100100"', f'id="{given}"'))


# --- saving --------------------------------------------------------------------


def test_a_rule_with_only_a_wazuh_copy_can_be_saved(ids):
    language, text, record = overlay._prepare({"wazuh": RULE})[0]
    assert language == "wazuh" and record.native_key == "100100"
    assert overlay.LAYOUT["wazuh"] == ("wazuh", ".xml")


def test_a_wazuh_copy_on_a_site_value_refuses_the_save(ids):
    bad = RULE.replace(
        "(?i)javascript:</field>",
        '(?i)javascript:</field><field name="data.dstip">10.20.30.40</field>',
    )
    with pytest.raises(overlay.Refused, match="wazuh copy fails lint") as raised:
        overlay._prepare({"wazuh": bad})
    assert raised.value.detail["lint"]["passed"] is False


def test_a_wazuh_copy_takes_no_exceptions():
    with pytest.raises(overlay.Refused, match="not kept for a wazuh copy"):
        overlay._wanted(
            {"wazuh": [{"match": {"agent.name": "a"}, "note": "n"}]}, {"wazuh": RULE}
        )


def test_the_save_tool_passes_the_wazuh_copy(monkeypatch):
    seen = {}

    def fake(*args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return {"saved": []}

    monkeypatch.setattr("core.detections.tools.save_rule", fake)
    tools = SecurityDetectionsTools.__new__(SecurityDetectionsTools)
    asyncio.run(tools.save_detection_rule(wazuh_xml=RULE))
    assert seen["args"][-1] == RULE
    assert seen["kwargs"]["proven_event_ids"] is None


# --- ids as the manager reads them, and files as Wazuh writes them --------------


@pytest.mark.parametrize(
    "given",
    ["0100001", "１００１０３", "100100 0", "+100100"],
    ids=["leading-zero", "fullwidth", "space", "sign"],
)
def test_an_id_not_written_as_six_ascii_digits_is_refused(ids, given):
    ids["loaded"] = {"100001"}
    with pytest.raises(overlay.Refused, match="ASCII digits with no leading zero"):
        overlay._wazuh_with_id(RULE.replace('id="100100"', f'id="{given}"'))


def test_loaded_ids_are_compared_as_numbers(ids):
    ids["loaded"] = {"100100"}
    ids["own"] = {"0100100"}  # the same rule, read as a number
    assert overlay._wazuh_with_id(RULE) == RULE
    ids["own"] = set()
    ids["loaded"] = {" 100100"}
    with pytest.raises(overlay.Refused, match="already loaded"):
        overlay._wazuh_with_id(RULE)


def test_a_leading_xml_declaration_is_accepted():
    text = '<?xml version="1.0" encoding="UTF-8"?>\n' + RULE
    record = readers.parse_file("auto", Path("r.xml"), "r.xml", text)
    assert record.native_key == "100100" and record.content["_content"] == text
    assert wazuh_xml.without_declaration("﻿" + text) == "\n" + RULE


def test_a_rule_tag_inside_a_comment_is_not_given_the_id(ids):
    text = '<!-- was <rule id="7" level="3"> -->\n' + _rule(
        "<match>x</match>", attrs='level="5"'
    )
    out = overlay._wazuh_with_id(text)
    assert out.startswith('<!-- was <rule id="7" level="3"> -->')
    assert wazuh_xml.rule_id(wazuh_xml.parse(out)) == "100000"


LOCAL_RULES = """<!-- Local rules -->
<group name="local,syslog,">
  <rule id="100001" level="5">
    <match>Failed password for</match>
    <description>sshd: failed password</description>
  </rule>
  <rule id="0100002" level="7">
    <if_sid>100001</if_sid>
    <description>sshd: repeated failures</description>
    <mitre><id>T1110</id></mitre>
  </rule>
</group>
<group name="windows,">
  <rule id="100003" level="10">
    <if_sid>61603</if_sid>
    <group>sysmon,</group>
    <description>Rundll32 proxying JavaScript</description>
  </rule>
</group>
"""


def test_a_ruleset_file_is_read_rule_by_rule(tmp_path):
    (tmp_path / "local_rules.xml").write_text(LOCAL_RULES)
    (tmp_path / "local_decoder.xml").write_text(
        '<decoder name="x"><prematch>x</prematch></decoder>'
    )
    records = readers.read_source({"id": "acme", "format": "wazuh"}, tmp_path)
    assert [(r.native_key, r.name) for r in records] == [
        ("100001", "sshd: failed password"),
        ("100002", "sshd: repeated failures"),  # read as the manager reads it
        ("100003", "Rundll32 proxying JavaScript"),
    ]
    assert {r.path for r in records} == {"local_rules.xml"}
    assert [r.logic["groups"] for r in records] == [
        ["local", "syslog"],
        ["local", "syslog"],
        ["windows", "sysmon"],
    ]
    assert records[1].techniques == ["T1110"]
    assert records[2].content["_content"].startswith('<rule id="100003"')


def test_a_single_rule_file_is_still_kept_as_written(tmp_path):
    (tmp_path / "vigil_100100.xml").write_text(RULE)
    (record,) = readers.read_source({"id": "acme", "format": "wazuh"}, tmp_path)
    assert record.content["_content"] == RULE
