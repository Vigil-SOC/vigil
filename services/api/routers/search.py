"""GET /api/search: one search box over alerts, cases, workflows, agents and skills.

Each item is scored by core.search.engine: its words, the security words that
mean the same, and an embedding model on the configured Ollama when one
answers. Without one (a normal install's default) the search is words only.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from core.config import get_settings
from core.deps import provide_workflows
from core.routing import Auth, RouterMeta
from core.search.engine import Doc, VectorCache, ollama_embedder, search
from core.storage.models import Case, Finding
from core.storage.models.finding import FindingMitrePrediction
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/search", tags=["search"], auth=Auth.REQUIRED)

# The newest alerts are searched; older ones are found by the Alerts filters.
MAX_ALERTS = 5000
CORPUS_TTL = 30.0
KINDS = ("alert", "case", "workflow", "agent", "skill")
# A predicted MITRE technique at or above this confidence is searchable on its alert.
TECHNIQUE_MIN = 0.5
# Entity-context keys that are not entities: the raw record has its own panel.
_ENTITY_SKIP_KEYS = frozenset({"source_evidence", "full_log"})


def entities_of(entity_context: Any) -> set[str]:
    """Every short scalar value in an entity context: host, user, ip, hash..."""
    out: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k not in _ENTITY_SKIP_KEYS:
                    walk(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                walk(v)
        elif isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and len(text) <= 200:
                out.add(text)

    walk(entity_context or {})
    return out


class _Index:
    """The docs searched, rebuilt at most every CORPUS_TTL, and their vectors."""

    def __init__(self) -> None:
        self.model: Optional[str] = None
        self.cache: Optional[VectorCache] = None
        self.corpus: Tuple[float, List[Doc]] = (0.0, [])
        self.lock = threading.Lock()

    def vectors(self) -> VectorCache:
        settings = get_settings()
        model = (settings.search_embed_model or "").strip()
        if self.cache is None or model != self.model:
            self.model = model
            self.cache = VectorCache(
                ollama_embedder(settings.ollama_url, model) if model else None
            )
        return self.cache


_index = _Index()


def _iso(t: Any) -> Optional[str]:
    return t.isoformat() if hasattr(t, "isoformat") else None


def _alerts(session) -> List[Doc]:
    rows = session.execute(
        select(
            Finding.finding_id,
            Finding.title,
            Finding.description,
            Finding.data_source,
            Finding.severity,
            Finding.entity_context,
            Finding.ai_enrichment,
            Finding.timestamp,
        )
        .order_by(Finding.timestamp.desc().nullslast())
        .limit(MAX_ALERTS)
    ).all()
    # the techniques each was scored as, so "data theft" finds a T1041 alert whose text never says so
    techniques: Dict[str, List[str]] = {}
    for fid, tid in session.execute(
        select(FindingMitrePrediction.finding_id, FindingMitrePrediction.technique_id)
        .where(
            FindingMitrePrediction.finding_id.in_([r[0] for r in rows]),
            FindingMitrePrediction.confidence >= TECHNIQUE_MIN,
        )
        .order_by(FindingMitrePrediction.technique_id)
    ).all():
        techniques.setdefault(fid, []).append(tid)
    docs = []
    for fid, title, desc, source, sev, ents, ai, ts in rows:
        summary = (ai or {}).get("summary") if isinstance(ai, dict) else None
        name = title or (desc or "").split(". ")[0][:120] or fid
        docs.append(
            Doc(
                kind="alert",
                id=fid,
                title=name,
                body=" ".join(
                    filter(
                        None,
                        [
                            desc,
                            summary if isinstance(summary, str) else None,
                            " ".join(techniques.get(fid, [])),
                            " ".join(sorted(entities_of(ents))),
                        ],
                    )
                ),
                subtitle=" · ".join(filter(None, [source, sev])),
                route={"screen": "overview", "search": f"?alert={fid}"},
                when=_iso(ts),
            )
        )
    return docs


def _cases(session) -> List[Doc]:
    rows = session.execute(
        select(
            Case.case_id,
            Case.title,
            Case.description,
            Case.status,
            Case.priority,
            Case.tags,
            Case.mitre_techniques,
            Case.notes,
            Case.updated_at,
        )
    ).all()
    docs = []
    for cid, title, desc, status, prio, tags, mitre, notes, updated in rows:
        note_text = " ".join(
            str(n.get("content", "")) for n in (notes or []) if isinstance(n, dict)
        )[:3000]
        docs.append(
            Doc(
                kind="case",
                id=cid,
                title=title or cid,
                body=" ".join(
                    filter(
                        None,
                        [desc, " ".join(tags or []), " ".join(mitre or []), note_text],
                    )
                ),
                subtitle=" · ".join(filter(None, [prio, status])),
                route={"case": cid},
                when=_iso(updated),
            )
        )
    return docs


def _workflows(service) -> List[Doc]:
    docs = []
    for w in service.list_workflows() if service else []:
        phases = w.get("phases") or []
        steps = " ".join(
            f"{p.get('name', '')} {p.get('purpose', '')} {' '.join(p.get('steps') or [])}"
            for p in phases
            if isinstance(p, dict)
        )
        name = w.get("name") or w["id"]
        if " " not in name and "-" in name:  # built-ins are named by their id
            name = name.replace("-", " ").capitalize()
        kind = "Custom" if w.get("source") == "custom" else "Built-in"
        count = f" · {len(phases)} step{'s' if len(phases) != 1 else ''}"
        docs.append(
            Doc(
                kind="workflow",
                id=w["id"],
                title=name,
                body=" ".join(
                    filter(
                        None,
                        [
                            w.get("description"),
                            w.get("use_case"),
                            " ".join(w.get("trigger_examples") or []),
                            steps,
                            " ".join(w.get("agents") or []),
                        ],
                    )
                ),
                subtitle=f"{kind} workflow" + (count if phases else ""),
                route={"screen": "workflows", "search": f"?workflow={w['id']}"},
            )
        )
    return docs


def _agents() -> List[Doc]:
    from services.api.routers.agents import agent_manager

    try:
        agent_manager.refresh_custom_agents()
    except Exception:  # noqa: BLE001 - the built-ins still answer
        pass
    docs = []
    for a in agent_manager.agents.values():
        kind = "Custom" if a.id.startswith("custom-") else "Built-in"
        docs.append(
            Doc(
                kind="agent",
                id=a.id,
                title=a.name,
                body=" ".join(
                    filter(
                        None,
                        [
                            a.description,
                            a.specialization,
                            " ".join(a.recommended_tools or []),
                        ],
                    )
                ),
                subtitle=f"{kind} agent · {a.specialization or ''}".rstrip(" ·"),
                route={"screen": "workflows", "search": f"?tab=agents&agent={a.id}"},
            )
        )
    return docs


def _skills() -> List[Doc]:
    from core.skills.skill_library import load_skills, skill_roots

    return [
        Doc(
            kind="skill",
            id=s.name,
            title=s.name.replace("-", " ").capitalize(),
            body=s.description,
            subtitle="Skill",
            route={"screen": "workflows", "search": f"?tab=skills&skill={s.name}"},
        )
        for s in load_skills(skill_roots())
    ]


def corpus(service=None, *, fresh: bool = False) -> List[Doc]:
    with _index.lock:
        at, docs = _index.corpus
        if docs and not fresh and time.monotonic() - at < CORPUS_TTL:
            return docs
        docs = []
        with unit_of_work() as session:
            for part in (_alerts, _cases):
                try:
                    docs += part(session)
                except (
                    Exception
                ) as e:  # noqa: BLE001 - one part should not blank the rest
                    logger.warning("search: %s unavailable: %s", part.__name__, e)
        for part, args in ((_workflows, (service,)), (_agents, ()), (_skills, ())):
            try:
                docs += part(*args)
            except Exception as e:  # noqa: BLE001
                logger.warning("search: %s unavailable: %s", part.__name__, e)
        _index.corpus = (time.monotonic(), docs)
        return docs


@router.get("")
def search_everything(
    q: str = Query(..., min_length=1, max_length=300),
    limit: int = Query(5, ge=1, le=25, description="Results per kind"),
    kinds: Optional[str] = Query(
        None, description="Comma-separated: alert, case, workflow, agent, skill"
    ),
    service=Depends(provide_workflows),
) -> Dict[str, Any]:
    """Alerts, cases, workflows, agents and skills that match, grouped by kind.

    Each hit says why it matched ("mentions …", "about exfiltration", "its id",
    "similar meaning") and where it opens.
    """
    wanted = set((kinds or ",".join(KINDS)).split(","))
    docs = [d for d in corpus(service) if d.kind in wanted]
    vectors = _index.vectors()
    hits, method = search(q, docs, vectors)
    # An exact name leads its kind, so the limit below cannot cut it off.
    exact = q.strip().lower()
    hits.sort(key=lambda h: (h.doc.title or "").strip().lower() != exact)
    groups: Dict[str, List[Dict[str, Any]]] = {k: [] for k in KINDS if k in wanted}
    counts: Dict[str, int] = {k: 0 for k in groups}
    for h in hits:
        counts[h.doc.kind] += 1
        if len(groups[h.doc.kind]) < limit:
            groups[h.doc.kind].append(
                {
                    "kind": h.doc.kind,
                    "id": h.doc.id,
                    "title": h.doc.title,
                    "subtitle": h.doc.subtitle,
                    "why": h.why,
                    "by": h.by,
                    "score": h.score,
                    "route": h.doc.route,
                    "when": h.doc.when,
                }
            )
    return {
        "q": q,
        "method": (
            f"embeddings ({_index.model})"
            if method == "embeddings"
            else "words and related security terms"
        ),
        "groups": groups,
        "counts": counts,
        "indexed": len(docs),
        "embedded": sum(1 for d in docs if vectors.get(d.key) is not None),
    }
