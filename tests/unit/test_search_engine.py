"""The search engine behind the top bar: words, related security terms, meaning."""

from core.search.engine import Doc, VectorCache, expand, search, word_score

DOCS = [
    Doc(
        "alert",
        "f-1",
        "Exfiltration over C2 channel (T1041)",
        "863 KB out from cr-14-lnx-devops to an unknown host",
        "zeek · high",
    ),
    Doc(
        "alert",
        "f-2",
        "LSASS memory read by rundll32",
        "credential dumping on CR-10-WS-FINANC (T1003.001)",
        "edr · critical",
    ),
    Doc(
        "alert",
        "f-3",
        "Spearphishing attachment opened",
        "Invoice_Q3_2026.docm from billing@invoices.example",
        "m365 · medium",
    ),
    Doc(
        "workflow",
        "wf-1",
        "Suspicious sign-in",
        "Check the sign-in against the user's history, revoke sessions with approval",
    ),
    Doc(
        "skill",
        "phishing-triage",
        "Phishing triage",
        "How to work a reported phishing email",
    ),
    Doc(
        "agent",
        "responder",
        "Response agent",
        "Plans containment: isolate a host, disable an account",
    ),
]


def ids(q, docs=DOCS, cache=None):
    hits, _ = search(q, docs, cache)
    return [h.doc.id for h in hits]


def test_plain_words_find_their_items():
    assert ids("lsass")[0] == "f-2"
    assert ids("Phishing triage")[0] == "phishing-triage"


def test_the_start_of_a_word_is_enough():
    assert ids("exfil") == ["f-1"]


def test_plain_language_lands_on_the_security_term():
    assert ids("data theft")[0] == "f-1"
    assert ids("stolen credentials")[0] == "f-2"
    assert ids("someone logging in from a weird place")[0] == "wf-1"
    assert "responder" in ids("contain host")


def test_each_hit_says_why():
    hits, method = search("data theft", DOCS)
    assert method == "words"
    assert hits[0].why == "about exfiltration"
    hits, _ = search("lsass", DOCS)
    assert hits[0].why == "mentions “lsass”"


def test_an_id_wins_outright():
    hits, _ = search("f-3", DOCS)
    assert hits[0].doc.id == "f-3" and hits[0].by == "id"


def test_nothing_unrelated_comes_back():
    assert ids("kubernetes pod") == []


def test_a_word_every_item_shares_does_not_carry_a_result():
    many = [
        Doc("alert", f"n-{i}", f"Detected thing {i} from dns data source")
        for i in range(30)
    ] + [DOCS[0]]
    assert ids("data theft", many) == ["f-1"]


def test_concepts_read_from_the_query():
    own, related = expand("password spraying")
    assert own == ["password", "spraying"] and "brute force" in related


def test_meaning_from_an_embedding_model_when_one_answers():
    # a fake model that puts "lateral" texts together, so a query with no shared words still lands
    def embed(texts):
        return [
            (
                [1.0, 0.0]
                if ("move between machines" in t or "psexec" in t.lower())
                else [0.0, 1.0]
            )
            for t in texts
        ]

    docs = DOCS + [
        Doc("alert", "f-4", "PsExec service installed on cr-19-fileserver", "")
    ]
    cache = VectorCache(embed)
    cache.fill(docs, wait=True)
    hits, method = search("attacker trying to move between machines", docs, cache)
    assert method == "embeddings"
    assert (
        hits[0].doc.id == "f-4"
        and hits[0].by == "meaning"
        and hits[0].why == "similar meaning"
    )


def test_a_model_that_is_down_is_left_alone_for_a_while():
    calls = []

    def down(texts):
        calls.append(len(texts))
        return None

    cache = VectorCache(down, cooldown=60)
    search("exfil", DOCS, cache)
    search("exfil", DOCS, cache)
    assert (
        len(calls) == 1
    )  # the query embed failed, so neither the fill nor the second search asked again
    assert not cache.available


def test_word_score_is_zero_without_a_match():
    assert word_score(DOCS[0], ["kubernetes"], {}, "kubernetes") == (0.0, "")


# A model that scores by a hand-set resemblance to the query, so ranking rules can be pinned.
def fake_model(resemblance):
    def embed(texts):
        out = []
        for t in texts:
            body = t.split(": ", 1)[-1]
            if t.startswith("search_query"):
                out.append([1.0, 0.0])
                continue
            c = next((v for k, v in resemblance.items() if k in body), 0.0)
            out.append([c, (1 - c * c) ** 0.5])
        return out

    return embed


EXFIL = Doc(
    "alert",
    "f-20261026-566a0516",
    "Large outbound transfer to cdn-sync-update.net",
    "15.2 MB from cr-14-lnx-devops over HTTPS (T1041)",
    "paloalto · high",
)
PHISH = [
    Doc(
        "alert",
        f"f-p{i}",
        "User reported phishing",
        f"Report {i} from the phish button",
    )
    for i in range(6)
]


def test_the_technique_in_the_body_outranks_a_loose_resemblance():
    cache = VectorCache(
        fake_model({"User reported phishing": 0.69, "Large outbound": 0.6})
    )
    docs = [EXFIL] + PHISH
    cache.fill(docs, wait=True)
    hits, method = search("data theft", docs, cache)
    assert method == "embeddings"
    assert hits[0].doc.id == EXFIL.id and hits[0].why == "about exfiltration"


def test_meaning_alone_must_be_near_the_best_match():
    near = Doc("alert", "f-near", "Archive staged then uploaded", "")
    far = Doc("alert", "f-far", "Printer offline", "")
    cache = VectorCache(fake_model({"Archive staged": 0.8, "Printer": 0.62}))
    cache.fill([near, far], wait=True)
    got = [
        h.doc.id
        for h in search("moving files out of the company", [near, far], cache)[0]
    ]
    assert got == ["f-near"]


def test_ids_hosts_and_accounts_are_looked_up_not_interpreted():
    asked = []

    def embed(texts):
        asked.extend(t for t in texts if t.startswith("search_query"))
        return [[1.0, 0.0] for _ in texts]  # everything resembles everything

    docs = [EXFIL] + PHISH
    cache = VectorCache(embed)
    cache.fill(docs, wait=True)
    for q in ("f-20261026-566a0516", "cr-14", "f.ortiz", "10.0.10.20"):
        hits, method = search(q, docs, cache)
        assert method == "words", q
        assert all(h.by != "meaning" for h in hits), q
    assert asked == []
    assert [h.doc.id for h in search("cr-14", docs, cache)[0]] == [EXFIL.id]
    hits, _ = search("f-20261026", docs, cache)
    assert hits[0].doc.id == EXFIL.id and hits[0].by == "id"


def test_stolen_credentials_reach_valid_account_use():
    doc = Doc(
        "alert",
        "f-va",
        "Logon with a service account from a new host",
        "T1078.002 on CR-10-WS-FINANC",
    )
    assert ids("stolen credentials", [doc]) == ["f-va"]
    _own, related = expand("stolen credentials")
    assert "t1078" in related["credential access"]


def test_meaning_alone_keeps_only_the_closest_few_per_kind():
    from core.search.engine import MEANING_ONLY_MAX

    # a routine alert repeated by the hundred, all close in meaning to the query
    many = [
        Doc("alert", f"f-r{i}", "Multiple authentication failures", f"Run {i}")
        for i in range(40)
    ]
    cache = VectorCache(fake_model({"Multiple authentication": 0.7}))
    cache.fill(many, wait=True)
    hits, method = search("account takeover attempts", many, cache)
    assert method == "embeddings"
    assert 0 < len(hits) <= MEANING_ONLY_MAX
    assert all(h.by == "meaning" for h in hits)


def test_no_model_is_words_only():
    # a normal install: no embedding model configured, or none answering
    hits, method = search("data theft", DOCS, VectorCache(None))
    assert method == "words" and hits[0].doc.id == "f-1"
    assert all(h.by != "meaning" for h in hits)


def test_an_ollama_that_does_not_answer_falls_back_to_words():
    from core.search.engine import ollama_embedder

    embed = ollama_embedder("http://127.0.0.1:9", "nomic-embed-text", timeout=0.5)
    assert embed(["anything"]) is None
    cache = VectorCache(embed)
    hits, method = search("lsass", DOCS, cache)
    assert method == "words" and hits[0].doc.id == "f-2"
    assert not cache.available and len(cache) == 0


def test_the_vector_cache_is_bounded_and_float32():
    docs = [Doc("alert", f"b-{i}", f"Alert number {i}") for i in range(30)]
    cache = VectorCache(lambda texts: [[0.5, 0.25] for _ in texts], max_entries=10)
    cache.fill(docs, wait=True)
    assert len(cache) == 10
    assert cache.get(docs[0].key) is None  # the oldest went first
    kept = cache.get(docs[-1].key)
    assert kept is not None and kept.typecode == "f" and list(kept) == [0.5, 0.25]


def test_a_host_is_looked_up_as_written_not_as_a_concept():
    docs = [
        Doc("alert", "f-9", "Beaconing to a rare domain", "periodic C2 callbacks"),
        Doc("alert", "f-10", "New service on c2-host-14", "c2-host-14 installed svc"),
    ]
    hits, _ = search("c2-host-14", docs)
    assert [h.doc.id for h in hits] == ["f-10"]
