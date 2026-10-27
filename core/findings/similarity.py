"""Group alerts that look like the same thing.

Two signals, scored together: what the alert says (its title and description)
and what it touches (hosts, users, addresses, hashes in its entity context).
The wording is compared with a TF-IDF cosine over the alerts' words, or with
embeddings when the caller passes an embedder; the response says which.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

TEXT_WEIGHT = 0.6
ENTITY_WEIGHT = 0.4
MIN_SCORE = 0.35

_WORD = re.compile(r"[a-z0-9][a-z0-9._-]+")
# Words every alert shares, so they say nothing about which alerts belong together.
_STOP = frozenset(
    "the a an and or of to from on in at by for with is was were be this that it as "
    "alert detected detection event activity suspicious possible new".split()
)
# Entity values too common to link two alerts (a domain controller is in every sign-in).
_ENTITY_SKIP = frozenset({"", "-", "none", "null", "unknown", "n/a", "true", "false"})
# Context keys that are not entities: the raw record (its own panel) and
# verdict-like fields every alert of a kind shares.
_KEY_SKIP = frozenset(
    {
        "source_evidence",
        "action",
        "protocol",
        "platform",
        "risk_level",
        "verdict",
        "rule_level",
        "service",
        "src_port",
        "dest_port",
        "bytes_out",
        "bytes_in",
        "duration_s",
        "flow_count",
    }
)


@dataclass
class Candidate:
    finding_id: str
    title: str = ""
    description: str = ""
    severity: Optional[str] = None
    data_source: Optional[str] = None
    timestamp: Optional[str] = None
    entities: set[str] = field(default_factory=set)
    # lower-cased entity -> as written, so reasons read "same FIN-WS10"
    spelled: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title}. {self.description}".strip()


def entities_of(
    entity_context: Any, spelled: Optional[dict[str, str]] = None
) -> set[str]:
    """Every scalar value in an entity context, lower-cased: host, user, ip, hash…"""
    out: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k not in _KEY_SKIP:
                    walk(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                walk(v)
        elif isinstance(value, (str, int)) and not isinstance(value, bool):
            raw = str(value).strip()
            text = raw.lower()
            if text not in _ENTITY_SKIP and len(text) <= 200:
                out.add(text)
                if spelled is not None:
                    spelled.setdefault(text, raw)

    walk(entity_context or {})
    return out


def candidate_from_row(row: dict) -> Candidate:
    ts = row.get("timestamp")
    spelled: dict[str, str] = {}
    entities = entities_of(row.get("entity_context"), spelled)
    return Candidate(
        finding_id=str(row.get("finding_id")),
        title=row.get("title") or "",
        description=row.get("description") or "",
        severity=row.get("severity"),
        data_source=row.get("data_source"),
        timestamp=ts.isoformat() if hasattr(ts, "isoformat") else ts,
        entities=entities,
        spelled=spelled,
    )


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def word_similarity(texts: list[str]) -> list[float]:
    """TF-IDF cosine of texts[0] against each of texts[1:]."""
    docs = [Counter(_tokens(t)) for t in texts]
    df: Counter = Counter()
    for d in docs:
        df.update(d.keys())
    n = len(docs)

    def vec(d: Counter) -> dict[str, float]:
        return {w: c * (math.log((1 + n) / (1 + df[w])) + 1) for w, c in d.items()}

    q = vec(docs[0])
    out = []
    for d in docs[1:]:
        v = vec(d)
        dot = sum(q[w] * v.get(w, 0.0) for w in q)
        nq = math.sqrt(sum(x * x for x in q.values()))
        nv = math.sqrt(sum(x * x for x in v.values()))
        out.append(dot / (nq * nv) if nq and nv else 0.0)
    return out


Embedder = Callable[[list[str]], Optional[list[list[float]]]]


def similar(
    target: Candidate,
    pool: Iterable[Candidate],
    *,
    embed: Optional[Embedder] = None,
    method_name: str = "embeddings",
    limit: int = 10,
    min_score: float = MIN_SCORE,
) -> dict:
    """The pool's alerts most like the target, best first, each with its reasons."""
    others = [c for c in pool if c.finding_id != target.finding_id]
    if not others:
        return {"method": "none", "similar": []}

    texts = [target.text] + [c.text for c in others]
    vectors = embed(texts) if embed else None
    if vectors:
        text_scores = [max(0.0, _cosine(vectors[0], v)) for v in vectors[1:]]
        method = method_name
    else:
        text_scores = word_similarity(texts)
        method = "word overlap"

    results = []
    for cand, text_score in zip(others, text_scores):
        shared = sorted(target.entities & cand.entities)
        entity_score = (
            len(shared) / min(len(target.entities), len(cand.entities))
            if shared
            else 0.0
        )
        score = TEXT_WEIGHT * text_score + ENTITY_WEIGHT * entity_score
        if score < min_score:
            continue
        shown = [target.spelled.get(v, v) for v in shared]
        reasons = [f"same {v}" for v in shown[:3]]
        if text_score >= 0.6:
            reasons.insert(0, "describes the same behaviour")
        elif text_score >= 0.35:
            reasons.insert(0, "similar description")
        results.append(
            {
                "finding_id": cand.finding_id,
                "title": cand.title,
                "severity": cand.severity,
                "data_source": cand.data_source,
                "timestamp": cand.timestamp,
                "score": round(score, 3),
                "text_score": round(text_score, 3),
                "shared_entities": shown,
                "reasons": reasons,
            }
        )
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"method": method, "similar": results[:limit]}
