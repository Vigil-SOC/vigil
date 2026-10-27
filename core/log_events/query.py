"""The Logs search language, read into SQL over ``log_events``.

Free words match anywhere in an event (summary, hosts, user, its raw record);
"quoted words" match as a phrase; ``key:value`` narrows one field; a leading
``-`` excludes; ``a,b`` and ``key:a,b`` match either; ``loglm>0.7`` keeps events the network AI scored at least that
unusual. Keys: source, host, ip, user, port, incident, mitre, sev, class,
action, id. Unknown keys are read as free text, so nothing typed is ignored.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, not_, or_

from core.storage.models.log_event import LogEvent

KEYS = (
    "source",
    "host",
    "ip",
    "user",
    "port",
    "incident",
    "mitre",
    "sev",
    "class",
    "action",
    "id",
)
_SCORE = re.compile(r"^loglm([<>]=?)([0-9.]+)$", re.I)
_KEYED = re.compile(r"^([a-z]+):(.+)$", re.I)

# Source keys and the words people use for them.
SOURCE_ALIASES = {
    "sysmon": ("sysmon",),
    "zeek": ("zeek", "loglm", "network"),
    "paloalto": ("paloalto", "palo", "pan", "firewall", "ngfw"),
    "dns": ("dns",),
    "winsec": ("winsec", "windows", "security", "ad", "identity"),
    "m365": ("m365", "defender", "email", "office"),
    "crowdstrike": ("crowdstrike", "falcon", "edr"),
    "entra": ("entra", "azuread", "aad", "signin"),
}


@dataclass
class Term:
    key: Optional[str]
    value: str
    negate: bool = False
    op: Optional[str] = None


def parse(q: str) -> List[Term]:
    try:
        tokens = shlex.split(q or "")
    except ValueError:  # an unclosed quote reads as plain words
        tokens = (q or "").replace('"', " ").split()
    terms: List[Term] = []
    for tok in tokens:
        negate = tok.startswith("-") and len(tok) > 1
        if negate:
            tok = tok[1:]
        m = _SCORE.match(tok)
        if m:
            terms.append(Term("loglm", m.group(2), negate, m.group(1)))
            continue
        m = _KEYED.match(tok)
        if m and m.group(1).lower() in KEYS:
            terms.append(Term(m.group(1).lower(), m.group(2), negate))
        else:
            terms.append(Term(None, tok, negate))
    return terms


def _source_keys(value: str) -> List[str]:
    v = value.lower()
    return [
        k
        for k, words in SOURCE_ALIASES.items()
        if k == v or any(w.startswith(v) or v.startswith(w) for w in words)
    ] or [v]


def _like(col, value: str):
    return col.ilike(f"%{value}%")


def condition(t: Term):
    if (
        t.key != "loglm" and "," in t.value and " " not in t.value
    ):  # sev:high,critical means either
        c = or_(*[condition(Term(t.key, v)) for v in t.value.split(",") if v])
        return not_(c) if t.negate else c
    v = t.value
    E = LogEvent
    if t.key == "source":
        c = E.source.in_(_source_keys(v))
    elif t.key == "host":
        c = or_(_like(E.src_host, v), _like(E.dst_host, v))
    elif t.key == "ip":
        c = (
            or_(E.src_ip == v, E.dst_ip == v)
            if not v.endswith(".") and v.count(".") == 3
            else or_(E.src_ip.startswith(v), E.dst_ip.startswith(v))
        )
    elif t.key == "user":
        c = _like(E.user_name, v)
    elif t.key == "port":
        c = E.dst_port == int(v) if v.isdigit() else E.dst_port.is_(None)
    elif t.key == "incident":
        # incident:* is any event tied to a case
        c = E.incident.isnot(None) if v == "*" else func.lower(E.incident) == v.lower()
    elif t.key == "mitre":
        c = E.mitre.ilike(f"{v}%")
    elif t.key == "sev":
        c = E.severity == v.lower()
    elif t.key == "class":
        c = E.event_class.ilike(f"{v}%")
    elif t.key == "action":
        c = func.lower(E.action) == v.lower()
    elif t.key == "id":
        c = E.event_id == v
    elif t.key == "loglm":
        try:
            n = float(v)
        except ValueError:
            n = 0.0
        c = {
            ">": E.loglm > n,
            ">=": E.loglm >= n,
            "<": E.loglm < n,
            "<=": E.loglm <= n,
        }[t.op or ">="]
    else:
        c = E.search_text.contains(v.lower())
    return not_(c) if t.negate else c


def where(q: str):
    terms = parse(q)
    return and_(*[condition(t) for t in terms]) if terms else None


def search_text(e: Dict[str, Any]) -> str:
    """What free words match: the readable fields and the raw record."""
    parts = [
        e.get("summary"),
        e.get("src_host"),
        e.get("dst_host"),
        e.get("src_ip"),
        e.get("dst_ip"),
        e.get("user_name"),
        e.get("action"),
        e.get("event_class"),
        e.get("mitre"),
        e.get("incident"),
        e.get("source"),
        " ".join(e.get("tags") or []),
        json.dumps(e.get("raw") or {}, ensure_ascii=False),
    ]
    return " ".join(str(p) for p in parts if p not in (None, "")).lower()


def to_row(e: LogEvent) -> Dict[str, Any]:
    """The event as the console's LogRow."""
    t = e.time
    return {
        "id": e.event_id,
        "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts": int(t.timestamp()),
        "source": e.source,
        "class": e.event_class,
        "severity": e.severity,
        "srcIp": e.src_ip,
        "srcHost": e.src_host,
        "dstIp": e.dst_ip,
        "dstHost": e.dst_host,
        "dstPort": e.dst_port,
        "user": e.user_name,
        "action": e.action,
        "summary": e.summary,
        "loglm": e.loglm,
        "mitre": e.mitre,
        "incident": e.incident,
        "tags": list(e.tags or []),
        "raw": e.raw or {},
    }


def to_record(e: Dict[str, Any]) -> Dict[str, Any]:
    """An ingested event, already validated, as a ``log_events`` row."""
    row = {
        "event_id": e["id"],
        "time": e["time"],
        "source": e["source"].lower(),
        "event_class": e.get("event_class") or "",
        "severity": (e.get("severity") or "info").lower(),
        "src_ip": e.get("src_ip"),
        "src_host": e.get("src_host"),
        "dst_ip": e.get("dst_ip"),
        "dst_host": e.get("dst_host"),
        "dst_port": e.get("dst_port"),
        "user_name": e.get("user"),
        "action": e.get("action"),
        "summary": e.get("summary") or "",
        "mitre": e.get("mitre"),
        "loglm": e.get("loglm"),
        "incident": e.get("incident"),
        "tags": list(e.get("tags") or []),
        "raw": e.get("raw") or {},
    }
    row["search_text"] = search_text(row)
    return row
