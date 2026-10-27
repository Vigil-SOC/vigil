"""Similar-alert grouping: the scoring, the reasons and the fallback."""

import asyncio

import pytest
from fastapi import HTTPException

from core.findings.similarity import Candidate, entities_of, similar, word_similarity


def _c(fid, title, desc="", ents=()):
    return Candidate(finding_id=fid, title=title, description=desc, entities=set(ents))


def test_entities_flatten_nested_context_and_skip_noise():
    ctx = {
        "host": "FIN-WS10",
        "users": ["F.Ortiz", "unknown"],
        "net": {"src_ip": "10.0.30.40"},
        "flag": True,
    }
    assert entities_of(ctx) == {"fin-ws10", "f.ortiz", "10.0.30.40"}


def test_word_similarity_ranks_shared_wording_higher():
    a, b = word_similarity(
        [
            "encoded powershell from winword",
            "winword spawned encoded powershell",
            "printer firmware check",
        ]
    )
    assert a > b


def test_similar_groups_by_behaviour_and_entities_with_reasons():
    target = _c(
        "t",
        "Encoded PowerShell from Office",
        "WINWORD spawned powershell -enc",
        {"fin-ws10", "f.ortiz"},
    )
    pool = [
        target,
        _c(
            "same-host",
            "Connection to newly registered domain",
            "fin-ws10 resolved cdn-sync-update.net",
            {"fin-ws10", "f.ortiz"},
        ),
        _c(
            "same-behaviour",
            "Encoded PowerShell from Office",
            "EXCEL spawned powershell -enc",
            {"hr-ws09"},
        ),
        _c("unrelated", "Printer firmware check", "vendor update", {"printer-2f"}),
    ]
    out = similar(target, pool, embed=None)
    ids = [r["finding_id"] for r in out["similar"]]
    assert out["method"] == "word overlap"
    assert "t" not in ids and "unrelated" not in ids
    assert set(ids) == {"same-host", "same-behaviour"}
    by = {r["finding_id"]: r for r in out["similar"]}
    assert "same fin-ws10" in by["same-host"]["reasons"]
    assert by["same-behaviour"]["reasons"][0] in {
        "describes the same behaviour",
        "similar description",
    }


def test_embeddings_are_used_when_available():
    target = _c("t", "a")
    pool = [_c("x", "b", ents=()), _c("y", "c")]
    vectors = {"a.": [1.0, 0.0], "b.": [0.9, 0.1], "c.": [0.0, 1.0]}
    out = similar(
        target,
        pool,
        embed=lambda texts: [vectors[t] for t in texts],
        method_name="embeddings (test)",
    )
    assert out["method"] == "embeddings (test)"
    assert [r["finding_id"] for r in out["similar"]] == ["x"]


def test_embedder_failure_falls_back_to_words():
    out = similar(
        _c("t", "beacon to new domain"),
        [_c("x", "beacon to new domain")],
        embed=lambda texts: None,
    )
    assert out["method"] == "word overlap" and out["similar"][0]["finding_id"] == "x"


def test_endpoint_compares_against_recent_findings_and_404s_an_unknown_one(
    monkeypatch,
):
    from services.api.routers import findings

    rows = [
        {
            "finding_id": "t",
            "title": "Encoded PowerShell from Office",
            "entity_context": {"host": "FIN-WS10"},
        },
        {
            "finding_id": "x",
            "title": "Beacon to new domain",
            "entity_context": {"host": "FIN-WS10"},
        },
        {"finding_id": "y", "title": "Printer firmware check"},
    ]

    class _Data:
        def get_finding(self, finding_id):
            return next((r for r in rows if r["finding_id"] == finding_id), None)

        def get_findings(self, limit):
            return rows[:limit]

    monkeypatch.setattr(findings, "data_service", _Data())
    out = asyncio.run(findings.similar_findings("t", limit=5, window=100))
    assert out["compared"] == 3
    assert [r["finding_id"] for r in out["similar"]] == ["x"]
    assert out["similar"][0]["reasons"] == ["same FIN-WS10"]

    with pytest.raises(HTTPException) as err:
        asyncio.run(findings.similar_findings("nope", limit=5, window=100))
    assert err.value.status_code == 404
