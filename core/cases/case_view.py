"""One case as files: what the case shell (#4199) reads.

The shell is handed this view as a tar; it is the only way an agent reads its
case's records and evidence (#4201). Every read
runs under the caller's claims, as any tool does. Renders are kept briefly per
case and caller, and a file's extracted text is kept by its hash, so a run that
calls the shell ten times extracts each attachment once.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import tarfile
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

Args = Dict[str, Any]

RENDER_TTL_S = 60.0
RENDERS_KEPT = 16
TEXTS_KEPT = 64
# Recall reads per key; a case naming hundreds of entities asks for the first.
HISTORY_KEYS_MAX = 50

README = """\
This directory is one Vigil case, read-only except scratch/.

  case.json             the case record
  findings/index.tsv    finding_id, severity, timestamp, title, one per line
  findings/<id>.json    every finding on the case, in full
  tasks.json            the case's tasks
  escalations.json      its escalations
  flows.json            per host pair sums of its network-flow findings (when any)
  evidence/index.json   each evidence item: id, name, type, hashes, and its file
  evidence/<id>-<name>.txt   that item's text, extracted from the original file
  entities/keys.txt     the entity keys its findings name
  entities/history.json what Vigil remembers about them (recall_entity)
  scratch/              writable for this one command; gone afterwards

A document condensed before a hunt read it is attached here as the original.
"""


def shell_address() -> Tuple[Optional[str], Optional[str]]:
    """The agent-shell sidecar's unix socket, else its URL; neither when absent."""
    return os.getenv("SHELL_SOCKET") or None, os.getenv("SHELL_URL") or None


class EvidenceUnreadable(Exception):
    """A missing file, a failed extraction, or an item with nothing to read."""


# ---------------------------------------------------------------------------
# Records: findings, tasks, escalations, flows
# ---------------------------------------------------------------------------


def findings_on_case(session: Any, case_id: str) -> Any:
    from core.storage.models import Finding, case_findings

    return (
        session.query(Finding)
        .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
        .filter(case_findings.c.case_id == case_id)
    )


def newest_first(on_case: Any) -> Any:
    from sqlalchemy.orm import noload

    from core.storage.models import Finding

    return on_case.options(noload(Finding.mitre_prediction_rows)).order_by(
        Finding.timestamp.desc().nulls_last(), Finding.finding_id
    )


def side_records(session: Any, case_id: str, on_case: Any) -> Args:
    """Tasks and escalations, then flows when the case holds any."""
    from core.cases.case_records_service import list_escalations, tasks_on_case
    from core.storage.schemas.case_entities import CaseEscalationSchema, CaseTaskSchema

    records: Args = {
        "tasks": CaseTaskSchema.dump_many(tasks_on_case(session, case_id)),
        "escalations": CaseEscalationSchema.dump_many(
            list_escalations(session, case_id)
        ),
    }
    flows = case_flows(on_case)
    if flows is not None:
        records["flows"] = flows
    return records


# Every NetFlow finding on the case, not a page: its ingest digest, read by JSON
# path so the flow previews stay in the database (#3275).
def case_flows(on_case: Any) -> Optional[Args]:
    from core.findings.netflow_digest import case_flow_summary
    from core.storage.models import Finding

    rows = on_case.with_entities(
        Finding.finding_id,
        Finding.entity_context["netflow_digest"],
        Finding.entity_context["source_evidence"]["telemetry_kind"],
    ).all()
    digested = [(fid, digest) for fid, digest, _ in rows if digest is not None]
    missing = [
        fid for fid, digest, kind in rows if digest is None and kind == "netflow"
    ]
    if not digested and not missing:
        return None
    return case_flow_summary(digested, without_digest=missing)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def evidence_summary(row: Any) -> Args:
    return {
        "evidence_id": row.evidence_id,
        "type": row.evidence_type,
        "name": row.name,
        "description": row.description,
        "has_file": bool(row.file_path),
        "size_bytes": row.file_size,
        "sha256": row.file_hash_sha256,
        "collected_by": row.collected_by,
        "collected_at": row.collected_at.isoformat() if row.collected_at else None,
    }


_texts: "OrderedDict[str, str]" = OrderedDict()
# Extractions run on several worker threads at once.
_texts_lock = threading.Lock()


def _extract(path: Any) -> str:
    """The file's text, extracted once per content hash."""
    from core.documents.extract import extract_document

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with _texts_lock:
        if digest in _texts:
            _texts.move_to_end(digest)
            return _texts[digest]
    text = extract_document(path, path.name).text
    with _texts_lock:
        _texts[digest] = text
        while len(_texts) > TEXTS_KEPT:
            _texts.popitem(last=False)
    return text


async def evidence_text(
    evidence_id: int, file_path: Optional[str], description: Optional[str]
) -> str:
    """One item's text: its file's, extracted, or its description when it has none."""
    from core.cases.case_evidence_service import CaseEvidenceService
    from core.documents.extract import DocumentRefused

    if not file_path:
        if not (description or "").strip():
            raise EvidenceUnreadable(f"evidence {evidence_id} has no file and no text")
        return description or ""
    try:
        path = CaseEvidenceService().resolve_stored_file(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"no file on disk at {file_path}")
        return await asyncio.to_thread(_extract, path)
    except (ValueError, OSError, DocumentRefused) as exc:
        raise EvidenceUnreadable(
            f"evidence {evidence_id}'s file could not be read: {exc}"
        ) from None


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------


def _safe(name: Any, keep: int = 80) -> str:
    return (
        re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._")[:keep] or "item"
    )


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False) + "\n"


def _records(case_id: str) -> Tuple[Optional[Args], Args, List[Args], List[Any]]:
    """The case, its side records, its findings in full, its evidence rows."""
    from core.cases.case_evidence_service import CaseEvidenceService
    from core.storage.database_data_service import get_database_data_service
    from core.storage.schemas import FindingSchema
    from core.storage.unit_of_work import unit_of_work

    case = get_database_data_service().get_case(case_id)
    if not case:
        return None, {}, [], []
    with unit_of_work() as session:
        on_case = findings_on_case(session, case_id)
        findings = FindingSchema.dump_many(newest_first(on_case).all())
        records = side_records(session, case_id, on_case)
        evidence = [
            (evidence_summary(row), row.file_path)
            for row in CaseEvidenceService().get_case_evidence(case_id, session=session)
        ]
    return case, records, findings, evidence


def _history(findings: List[Args]) -> Tuple[List[str], Optional[Args]]:
    from core.cases.linked_entities import linked_entity_keys
    from core.memory.recall import recall_entity

    try:
        keys = linked_entity_keys(f.get("entity_context") for f in findings)
        keys = keys[:HISTORY_KEYS_MAX]
        if not keys:
            return keys, None
        return keys, recall_entity({"entity_keys": keys, "caller_kind": "case_shell"})
    except Exception as exc:  # noqa: BLE001 - the rest of the view still serves
        return [], {"unavailable": f"entity history could not be read: {exc}"}


async def _evidence_files(evidence: List[Any]) -> Tuple[List[Args], Dict[str, str]]:
    async def one(summary: Args, file_path: Optional[str]) -> Tuple[Args, str, str]:
        name = f"evidence/{summary['evidence_id']}-{_safe(summary['name'])}.txt"
        try:
            text = await evidence_text(
                summary["evidence_id"], file_path, summary["description"]
            )
        except EvidenceUnreadable as exc:
            return {**summary, "file": None, "unavailable": str(exc)}, "", ""
        entry = {
            **summary,
            "file": name,
            "source": "file" if file_path else "description",
            "chars": len(text),
            "lines": text.count("\n") + 1,
        }
        return entry, name, text

    done = await asyncio.gather(*(one(s, p) for s, p in evidence))
    index = [entry for entry, _, _ in done]
    return index, {name: text for _, name, text in done if name}


def _tar(files: Dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for directory in sorted({n.rsplit("/", 1)[0] for n in files if "/" in n}):
            info = tarfile.TarInfo(directory)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, now
            tar.addfile(info)
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, now
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


_renders: "OrderedDict[Tuple[str, str, str], Tuple[float, bytes]]" = OrderedDict()


# Person and agent both: recall logs its read against the agent it ran for.
def _caller() -> Tuple[str, str]:
    from core.agents.attribution import current_claims

    claims = current_claims()
    if claims is None:
        return "", ""
    return str(claims.username or claims.person_id or ""), str(claims.agent_id or "")


async def render_case_view(case_id: str) -> Optional[bytes]:
    """The case as a tar, or None when there is no such case."""
    key = (case_id, *_caller())
    held = _renders.get(key)
    if held and held[0] > time.monotonic():
        return held[1]

    case, records, findings, evidence = await asyncio.to_thread(_records, case_id)
    if case is None:
        return None
    keys, history = await asyncio.to_thread(_history, findings)
    index, texts = await _evidence_files(evidence)

    files: Dict[str, str] = {"README.txt": README, "case.json": _json(case)}
    files["findings/index.tsv"] = "".join(
        "\t".join(
            str(f.get(k) or "").replace("\t", " ").replace("\n", " ")
            for k in ("finding_id", "severity", "timestamp", "title")
        )
        + "\n"
        for f in findings
    )
    for finding in findings:
        files[f"findings/{_safe(finding.get('finding_id'), 120)}.json"] = _json(finding)
    for name in ("tasks", "escalations", "flows"):
        if name in records:
            files[f"{name}.json"] = _json(records[name])
    files["evidence/index.json"] = _json(index)
    files.update(texts)
    files["entities/keys.txt"] = "".join(f"{k}\n" for k in keys)
    if history is not None:
        files["entities/history.json"] = _json(history)

    view = _tar(files)
    _renders[key] = (time.monotonic() + RENDER_TTL_S, view)
    _renders.move_to_end(key)
    while len(_renders) > RENDERS_KEPT:
        _renders.popitem(last=False)
    return view
