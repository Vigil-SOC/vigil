"""The translated queries on a real OpenSearch over wazuh-archives-style documents.
Set VIGIL_TEST_OPENSEARCH_URL to a throwaway node, e.g. http://127.0.0.1:59261
(opensearchproject/opensearch with DISABLE_SECURITY_PLUGIN=true)."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core.detections.inventory.wazuh_shadow import ARCHIVES, translate
from tests.unit.test_wazuh_shadow import _rule

URL = os.environ.get("VIGIL_TEST_OPENSEARCH_URL")
pytestmark = pytest.mark.skipif(not URL, reason="VIGIL_TEST_OPENSEARCH_URL not set")

INDEX = "wazuh-archives-4.x-2026.11.07"
KEYWORD = {"type": "keyword"}
# The indexer's wazuh-template.json: full_log is analysed text, the rest keyword.
MAPPING = {
    "dynamic_templates": [
        {"strings": {"match_mapping_type": "string", "mapping": KEYWORD}}
    ],
    "properties": {
        "@timestamp": {"type": "date"},
        "full_log": {"type": "text"},
        "location": KEYWORD,
        "decoder": {"properties": {"name": KEYWORD}},
        "predecoder": {"properties": {"program_name": KEYWORD}},
    },
}
AT = datetime.now(timezone.utc) - timedelta(minutes=5)
DOCS = [
    {
        "full_log": "Nov 7 sshd[1]: Failed password for root from 10.0.0.1",
        "location": "/var/log/auth.log",
        "decoder": {"name": "sshd"},
        "predecoder": {"program_name": "sshd"},
        "data": {"srcip": "10.0.0.1", "dstuser": "root", "id": "4625"},
    },
    {
        "full_log": "Nov 7 sshd[2]: Failed password for admin from 10.0.0.2",
        "location": "/var/log/auth.log",
        "decoder": {"name": "sshd"},
        "predecoder": {"program_name": "sshd"},
        "data": {"srcip": "10.0.0.2", "dstuser": "admin"},
    },
    {
        "full_log": "GET / 200",
        "location": "/var/log/apache.log",
        "decoder": {"name": "web-accesslog"},
        "predecoder": {"program_name": "apache2"},
        "data": {"win": {"eventdata": {"image": "C:\\Windows\\a.exe"}}},
    },
]


@pytest.fixture(scope="module")
def node():
    with httpx.Client(base_url=URL, timeout=30) as http:
        http.delete(f"/{INDEX}")
        http.put(f"/{INDEX}", json={"mappings": MAPPING}).raise_for_status()
        for n, doc in enumerate(DOCS):
            http.put(
                f"/{INDEX}/_doc/{n}", json={**doc, "@timestamp": AT.isoformat()}
            ).raise_for_status()
        http.post(f"/{INDEX}/_refresh").raise_for_status()
        yield http
        http.delete(f"/{INDEX}")


def _count(body):
    from core.integrations.elastic.client import ElasticService

    query = translate(_rule(body))
    assert query.reasons == [], query.reasons

    async def go():
        service = ElasticService(elasticsearch_url=URL)
        window = {
            "@timestamp": {
                "gte": (AT - timedelta(hours=1)).isoformat(),
                "lt": (AT + timedelta(hours=1)).isoformat(),
            }
        }
        try:
            return await service.dsl_search(ARCHIVES, query.query, window, 100)
        finally:
            await service.close()

    total, docs = asyncio.run(go())
    assert len(docs) == total
    return total


@pytest.mark.parametrize(
    "body, hits",
    [
        ("<decoded_as>sshd</decoded_as>", 2),
        ("<decoded_as>sshd</decoded_as><srcip>10.0.0.1</srcip>", 1),
        ("<program_name>ssh</program_name>", 2),  # OSMatch finds it anywhere
        ("<program_name>^ssh</program_name>", 2),
        ("<program_name>sshd$</program_name>", 2),
        ("<program_name>^sshd$</program_name>", 2),
        ("<program_name>^shd</program_name>", 0),
        ("<program_name>apache2|sshd</program_name>", 3),
        ("<location>^/var/log/auth.log$</location>", 2),
        ("<user>root</user>", 1),
        ("<id>4625</id><decoded_as>sshd</decoded_as>", 1),
        ('<field name="win.eventdata.image">^C:</field>', 1),
        ("<decoded_as>nginx</decoded_as>", 0),
        ("<srcip>10.0.0.9</srcip>", 0),
        ("<program_name>SSHD</program_name>", 0),  # case-sensitive, as Wazuh is
        ("<decoded_as>sshd</decoded_as><user>nobody</user>", 0),
    ],
)
def test_the_translated_query_counts_the_right_events(node, body, hits):
    assert _count(body) == hits


def test_a_wildcard_character_in_a_value_is_literal(node):
    assert _count("<program_name>*</program_name>") == 0
    assert _count("<program_name>ssh?</program_name>") == 0
