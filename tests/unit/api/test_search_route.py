"""GET /api/search without a database: grouping, routes, and words-only by default."""

from types import SimpleNamespace

import pytest

from core.search.engine import Doc
from services.api.routers import search as search_router

DOCS = [
    Doc(
        "alert",
        "f-1",
        "Exfiltration over C2 channel (T1041)",
        "863 KB out from build-host-14",
        "zeek · high",
        route={"screen": "overview", "search": "?alert=f-1"},
    ),
    Doc(
        "skill",
        "phishing-triage",
        "Phishing triage",
        "How to work a reported phishing email",
        "Skill",
        route={"screen": "workflows", "search": "?tab=skills&skill=phishing-triage"},
    ),
    Doc(
        "agent",
        "triage",
        "Triage agent",
        "Sorts new alerts by how urgent they are",
        route={"screen": "workflows", "search": "?tab=agents&agent=triage"},
    ),
]


@pytest.fixture
def index(monkeypatch):
    def use(model: str, url: str = "http://127.0.0.1:9"):
        monkeypatch.setattr(
            search_router,
            "get_settings",
            lambda: SimpleNamespace(search_embed_model=model, ollama_url=url),
        )
        monkeypatch.setattr(search_router, "_index", search_router._Index())
        monkeypatch.setattr(
            search_router, "corpus", lambda service=None, fresh=False: DOCS
        )

    return use


def ask(q, kinds=None):
    return search_router.search_everything(q=q, limit=5, kinds=kinds, service=None)


@pytest.mark.unit
def test_groups_hits_by_kind_with_why_and_route(index):
    index("")
    body = ask("data theft")
    assert body["method"] == "words and related security terms"
    assert set(body["groups"]) == {"alert", "case", "workflow", "agent", "skill"}
    (alert,) = body["groups"]["alert"]
    assert alert["id"] == "f-1" and alert["why"] == "about exfiltration"
    assert alert["route"] == {"screen": "overview", "search": "?alert=f-1"}
    assert body["counts"]["alert"] == 1 and body["embedded"] == 0


@pytest.mark.unit
def test_kinds_limits_the_groups(index):
    index("")
    body = ask("triage", kinds="agent")
    assert set(body["groups"]) == {"agent"}
    assert body["groups"]["agent"][0]["id"] == "triage"


@pytest.mark.unit
def test_the_default_model_with_no_ollama_answering_is_words_only(index):
    index("nomic-embed-text")
    body = ask("phishing triage")
    assert body["method"] == "words and related security terms"
    assert body["groups"]["skill"][0]["id"] == "phishing-triage"
    assert all(h["by"] != "meaning" for g in body["groups"].values() for h in g)


@pytest.mark.unit
def test_entities_are_short_scalar_values():
    got = search_router.entities_of(
        {
            "hostnames": ["ws-1"],
            "user": "f.ortiz",
            "port": 443,
            "flag": True,
            "source_evidence": {"raw": "not an entity"},
        }
    )
    assert got == {"ws-1", "f.ortiz", "443"}


@pytest.mark.unit
def test_an_exact_title_survives_the_per_kind_limit(monkeypatch):
    docs = [
        Doc("case", f"INC-{n}", f"Phishing triage report {n}", "", when=f"2026-10-2{n}")
        for n in range(1, 7)
    ] + [Doc("case", "INC-0", "Phishing triage", "", when="2026-01-01")]
    monkeypatch.setattr(search_router, "_index", search_router._Index())
    monkeypatch.setattr(
        search_router,
        "get_settings",
        lambda: SimpleNamespace(search_embed_model="", ollama_url="http://127.0.0.1:9"),
    )
    monkeypatch.setattr(search_router, "corpus", lambda service=None, fresh=False: docs)
    out = search_router.search_everything(
        q="Phishing triage", limit=5, kinds="case", service=None
    )
    assert out["groups"]["case"][0]["id"] == "INC-0"
