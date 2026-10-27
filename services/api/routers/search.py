"""GET /api/search: one search box over alerts, cases, workflows, agents and skills.

Each is scored by core.search.engine on its words and the security words that
mean the same ("data theft" finds an exfiltration alert). No model is called:
the engine takes an embedder, and none is wired here, so search answers the
same on every install.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from core.deps import provide_workflows
from core.routing import Auth, RouterMeta
from core.search.engine import Doc, search
from core.storage.models import Case, Finding
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/search", tags=["search"], auth=Auth.REQUIRED)

MAX_ALERTS = 5000
CORPUS_TTL = 30.0
KINDS = ("alert", "case", "workflow", "agent", "skill")
# A predicted MITRE technique at or above this relevance is searchable on its alert.
TECHNIQUE_MIN = 0.5

_corpus: Tuple[float, List[Doc]] = (0.0, [])
_corpus_lock = threading.Lock()


def _entity_words(context: Any) -> List[str]:
    """Every string value in an alert's entity context: hosts, users, addresses, hashes."""
    out: List[str] = []
    if isinstance(context, dict):
        for value in context.values():
            out += _entity_words(value)
    elif isinstance(context, list):
        for value in context:
            out += _entity_words(value)
    elif isinstance(context, str) and context.strip():
        out.append(context.strip())
    return out


def _iso(t: Any) -> Optional[str]:
    return t.isoformat() if hasattr(t, "isoformat") else None


def _alerts(session) -> List[Doc]:
    from core.storage.models.finding import FindingMitrePrediction

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
                            summary,
                            " ".join(techniques.get(fid, [])),
                            " ".join(sorted(set(_entity_words(ents)))),
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
                subtitle=f"{'Custom' if w.get('source') == 'custom' else 'Built-in'} workflow"
                + (
                    f" · {len(phases)} step{'s' if len(phases) != 1 else ''}"
                    if phases
                    else ""
                ),
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
    return [
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
            subtitle=f"{'Custom' if a.id.startswith('custom-') else 'Built-in'} agent · {a.specialization or ''}".rstrip(
                " ·"
            ),
            route={"screen": "workflows", "search": f"?tab=agents&agent={a.id}"},
        )
        for a in agent_manager.agents.values()
    ]


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
    global _corpus
    with _corpus_lock:
        at, docs = _corpus
        if docs and not fresh and time.monotonic() - at < CORPUS_TTL:
            return docs
        docs = []
        with unit_of_work() as session:
            for part in (_alerts, _cases):
                try:
                    docs += part(session)
                except (
                    Exception
                ) as e:  # noqa: BLE001 - one missing table should not blank the rest
                    logger.warning("search: %s unavailable: %s", part.__name__, e)
        for part, args in ((_workflows, (service,)), (_agents, ()), (_skills, ())):
            try:
                docs += part(*args)
            except Exception as e:  # noqa: BLE001
                logger.warning("search: %s unavailable: %s", part.__name__, e)
        _corpus = (time.monotonic(), docs)
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
    wanted = set((kinds or ",".join(KINDS)).split(","))
    docs = [d for d in corpus(service) if d.kind in wanted]
    hits, _method = search(q, docs)
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
        "method": "words and related security terms",
        "groups": groups,
        "counts": counts,
        "indexed": len(docs),
    }
