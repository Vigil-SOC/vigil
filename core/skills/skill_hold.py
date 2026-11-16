"""Which operator skills agents may see: the scan verdict for the folder as it is now.

A skill is allowed when it is bundled, when scanning is switched off, when its
current digest scanned SAFE, or when someone approved that digest. Anything else
is held: an agent's index, its ``read_skill`` tool and every prompt that quotes a
skill leave it out. The verdict belongs to a digest, so editing any file in the
folder puts the skill back to pending until the sweep rescans it.

States, for the folder as it is now:

- ``bundled``   shipped in the library, never scanned, always allowed
- ``unchecked`` scanning is off by setting, an explicit opt-out: allowed
- ``pending``   no verdict for this digest yet, or the database could not be read: held
- ``failed``    the scan ran and produced no verdict: held
- ``safe``      the scan said SAFE: allowed
- ``flagged``   CAUTION or DO_NOT_INSTALL and nobody approved it: held
- ``approved``  a person released this digest: allowed
- ``refused``   the folder contains a symlink: held, never scanned, never approvable

``load_skills`` runs on every chat message, so ``agent_skills`` makes one query
and digests are cached against file sizes and modification times.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select

from core.config import Settings, get_settings
from core.skills.skill_library import Skill, is_bundled, load_skills, skill_roots
from core.skills.skill_scan import (
    STATUS_FAILED,
    STATUS_SCANNED,
    STATUS_UNAVAILABLE,
    scan_skill,
    skill_digest,
)
from core.storage.models import SkillScan
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

BUNDLED = "bundled"
UNCHECKED = "unchecked"
PENDING = "pending"
FAILED = "failed"
SAFE = "safe"
FLAGGED = "flagged"
APPROVED = "approved"
REFUSED = "refused"

ALLOWED = frozenset({BUNDLED, UNCHECKED, SAFE, APPROVED})

# A failed scan is tried again after this long, so a scanner that was down or a
# skill that timed out is not hammered every sweep.
RETRY_FAILED_AFTER = timedelta(hours=1)

# Set at shutdown so a sweep stops between skills instead of scanning on, which
# would hold the process open for the rest of the folder.
cancel = threading.Event()


@dataclass(frozen=True)
class ScanState:
    state: str
    recommendation: Optional[str] = None
    score: Optional[int] = None
    max_severity: Optional[str] = None
    findings: List[Dict[str, Any]] = field(default_factory=list)
    scanner_version: Optional[str] = None
    error: Optional[str] = None
    scanned_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None

    @property
    def allowed(self) -> bool:
        return self.state in ALLOWED


def scanning_on(settings: Optional[Settings] = None) -> bool:
    return bool(((settings or get_settings()).vigil_skill_scanner or "").strip())


# Digest cache: reading every file of every operator skill per chat message
# would be wasteful. A file's relative path, size, mtime, inode and ctime stand
# in for its bytes. mtime alone can be put back by hand after an edit; ctime is
# set by the kernel on every write or utime and cannot be, and the inode changes
# when a file is replaced. The signature costs one stat per file, the digest a
# full read, paid only after a change.
_Signature = Tuple[Tuple[str, int, int, int, int], ...]
_digests: Dict[str, Tuple[_Signature, str]] = {}
_digests_lock = threading.Lock()


def _signature(folder: Path) -> _Signature:
    entries = []
    for path in folder.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        entries.append(
            (
                path.relative_to(folder).as_posix(),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ino,
                stat.st_ctime_ns,
            )
        )
    return tuple(sorted(entries))


REFUSED_REASON = "contains a symlink"


def contains_symlink(folder: Path) -> bool:
    """Whether the folder, or anything inside it at any depth, is a symlink.

    The digest skips links but the loader and ``read_skill`` follow them, so a
    link could swap what an agent reads without moving the digest. Such a folder
    is refused outright rather than reasoned about.
    """
    if folder.is_symlink():
        return True
    return any(path.is_symlink() for path in folder.rglob("*"))


def digest_of(folder: Path) -> str:
    key = str(folder.resolve())
    signature = _signature(folder)
    with _digests_lock:
        cached = _digests.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    digest = skill_digest(folder)
    with _digests_lock:
        _digests[key] = (signature, digest)
    return digest


def _decide(row: Optional[SkillScan]) -> ScanState:
    if row is None:
        return ScanState(PENDING)
    common = dict(
        recommendation=row.recommendation,
        score=row.score,
        max_severity=row.max_severity,
        findings=list(row.findings or []),
        scanner_version=row.scanner_version,
        error=row.error,
        scanned_at=row.scanned_at,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
    )
    if row.approved_at is not None:
        return ScanState(APPROVED, **common)
    if row.status != STATUS_SCANNED:
        return ScanState(FAILED, **common)
    if row.recommendation == "SAFE":
        return ScanState(SAFE, **common)
    return ScanState(FLAGGED, **common)


def scan_states(
    skills: Iterable[Skill], settings: Optional[Settings] = None
) -> Dict[str, ScanState]:
    """The state of each skill by name, with at most one database query.

    A skill whose folder cannot be read, or whose verdicts the database cannot
    give, is pending: unknown content is held, not passed.
    """
    on = scanning_on(settings)
    states: Dict[str, ScanState] = {}
    digests: Dict[str, str] = {}
    for skill in skills:
        if is_bundled(skill):
            states[skill.name] = ScanState(BUNDLED)
        elif not on:
            states[skill.name] = ScanState(UNCHECKED)
        else:
            try:
                if contains_symlink(skill.path):
                    states[skill.name] = ScanState(REFUSED, error=REFUSED_REASON)
                    continue
                digests[skill.name] = digest_of(skill.path)
            except OSError as exc:
                logger.warning("skill %s could not be digested: %s", skill.name, exc)
                states[skill.name] = ScanState(PENDING)
    if digests:
        rows: Dict[str, SkillScan] = {}
        try:
            with unit_of_work() as session:
                found = session.execute(
                    select(SkillScan).where(SkillScan.digest.in_(set(digests.values())))
                ).scalars()
                for row in found:
                    session.expunge(row)
                    rows[row.digest] = row
        except Exception:  # noqa: BLE001 - fail closed, the next call asks again
            logger.exception("skill scan verdicts unreadable; holding operator skills")
        for name, digest in digests.items():
            states[name] = _decide(rows.get(digest))
    return states


def agent_skills(
    roots: Optional[Iterable[Path]] = None, settings: Optional[Settings] = None
) -> List[Skill]:
    """The loaded skills an agent may see, in ``load_skills`` order."""
    skills = load_skills(skill_roots(settings) if roots is None else roots)
    states = scan_states(skills, settings)
    return [s for s in skills if states[s.name].allowed]


def _row_for(name: str, result) -> Dict[str, Any]:
    return dict(
        skill_name=name,
        status=result.status,
        recommendation=result.recommendation,
        score=result.score,
        max_severity=result.max_severity,
        findings=[
            {
                "rule_id": f.rule_id,
                "severity": f.severity,
                "message": f.message,
                "file": f.file,
            }
            for f in result.findings
        ],
        scanner_version=result.scanner_version,
        error=result.error,
        scanned_at=utcnow(),
    )


def _record(digest: str, name: str, result) -> None:
    values = _row_for(name, result)
    with unit_of_work() as session:
        row = session.get(SkillScan, digest)
        if row is None:
            session.add(SkillScan(digest=digest, **values))
            return
        if row.status == STATUS_SCANNED:
            return  # another replica already has a verdict for this content
        # A failed row is retried in place; approval fields, if any, stay.
        for key, value in values.items():
            setattr(row, key, value)


def sweep(
    roots: Optional[Iterable[Path]] = None, settings: Optional[Settings] = None
) -> int:
    """Scan every operator skill that has no verdict for its current digest.

    One at a time. Returns how many scans produced a row. A failed row is tried
    again once ``RETRY_FAILED_AFTER`` has passed. With the scanner configured but
    missing, nothing is recorded and the skills stay pending.
    """
    settings = settings or get_settings()
    if not scanning_on(settings):
        return 0
    skills = [
        s
        for s in load_skills(skill_roots(settings) if roots is None else roots)
        if not is_bundled(s)
    ]
    if not skills:
        return 0
    digests = {}
    for skill in skills:
        if cancel.is_set():
            return 0
        try:
            if contains_symlink(skill.path):
                continue  # refused, not scanned: see scan_states
            digests[skill.name] = digest_of(skill.path)
        except OSError as exc:
            logger.warning("skill %s could not be digested: %s", skill.name, exc)
    with unit_of_work() as session:
        known = {
            d: (status, at)
            for d, status, at in session.execute(
                select(SkillScan.digest, SkillScan.status, SkillScan.scanned_at).where(
                    SkillScan.digest.in_(set(digests.values()))
                )
            )
        }
    now = utcnow()
    recorded = 0
    for skill in skills:
        if cancel.is_set():
            return recorded
        digest = digests.get(skill.name)
        if digest is None:
            continue
        seen = known.get(digest)
        if seen is not None and (
            seen[0] == STATUS_SCANNED or now - seen[1] < RETRY_FAILED_AFTER
        ):
            continue
        result = scan_skill(skill.path, settings)
        if result.status == STATUS_UNAVAILABLE:
            logger.warning(
                "skill scanner is configured but not usable (%s); operator skills "
                "stay held until it is",
                result.error,
            )
            return recorded
        if result.status not in (STATUS_SCANNED, STATUS_FAILED):
            return recorded
        try:
            if digest_of(skill.path) != digest:
                continue  # edited while it was scanned: the next sweep takes it
        except OSError:
            continue
        _record(digest, skill.name, result)
        recorded += 1
        logger.info(
            "skill %s scanned: %s%s",
            skill.name,
            result.recommendation or result.status,
            f" score={result.score}" if result.score is not None else "",
        )
        if result.status == STATUS_FAILED:
            logger.warning("skill %s scan failed: %s", skill.name, result.error)
    return recorded
