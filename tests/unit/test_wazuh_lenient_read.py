"""Wazuh's own parser reads a stray "<" in element text as text. Vigil reads
the same files: a stock rule file with one is listed, and a file Vigil still
cannot read is reported by name and reason, not dropped silently."""

import logging
import xml.etree.ElementTree as ET  # nosec B405

from core.detections import wazuh_xml
from core.detections.inventory import readers

# Rules 91003 and its neighbour from Wazuh 4.9.2's 0910-ms-exchange-proxylogon.
PROXYLOGON = r"""<group name="ms_exchange,proxylogon,">
  <rule id="91002" level="12">
    <decoded_as>ECPServerLog</decoded_as>
    <regex type="pcre2">Set-OabVirtualDirectory</regex>
    <description>MS Exchange - ProxyLogon, OAB virtual directory set.</description>
  </rule>
  <rule id="91003" level="12">
    <decoded_as>ECPServerLog</decoded_as>
    <regex type="pcre2">Set-.+VirtualDirectory.+?Url.+\<\w+.*\>.*?\<\/\w+\>.+?VirtualDirectory</regex>
    <description>MS Exchange - Possible ProxyLogon vulnerability exploitation (CVE-2021-27065).</description>
    <mitre>
      <id>T1210</id>
    </mitre>
  </rule>
</group>
"""


def test_stray_lt_in_a_regex_lists_every_rule():
    records = readers.wazuh_records(PROXYLOGON, "0910.xml")
    assert [r.native_key for r in records] == ["91002", "91003"]
    # The stored copy is well-formed XML that reads back as the regex written.
    stored = ET.fromstring(records[1].content["_content"])
    assert stored.findtext("regex") == (
        r"Set-.+VirtualDirectory.+?Url.+\<\w+.*\>.*?\<\/\w+\>.+?VirtualDirectory"
    )


def test_stray_lt_leaves_markup_alone():
    text = '<!-- a < b --><group name="g"><rule id="1"><regex>a \\< b</regex>'
    text += "<description><![CDATA[x < y]]></description></rule></group>"
    ((rule, _),) = wazuh_xml.parse_all(text)
    assert rule.findtext("regex") == "a \\< b"
    assert rule.findtext("description") == "x < y"


def test_a_file_still_unreadable_is_reported_by_name_and_reason(tmp_path, caplog):
    (tmp_path / "good.xml").write_text(PROXYLOGON)
    (tmp_path / "broken.xml").write_text('<group name="g"><rule id="1"></group>')
    with caplog.at_level(logging.WARNING):
        records = readers.read_source({"id": "acme", "format": "wazuh"}, tmp_path)
    assert [r.native_key for r in records] == ["91002", "91003"]
    (message,) = [m for m in caplog.messages if "broken.xml" in m]
    assert "not well-formed XML" in message
