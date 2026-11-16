"""Scan a skill folder with NVIDIA SkillSpector, run as a separate program.

SkillSpector lives in its own virtualenv and is invoked as a subprocess with a
time limit, never imported: its dependency tree must not mix with the backend's,
and a scan of untrusted content should not run inside the API process. Only the
static analyzers run (``--no-llm``), and the child gets a minimal environment, so
it cannot call a model whatever the host has configured. Its one network use,
the osv.dev dependency lookup, is off unless a setting turns it on.

``scan_skill`` never raises. It returns a ``ScanResult`` whose ``status`` says
whether a verdict exists; only ``scanned`` carries one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

SCAN_TIMEOUT_SECONDS = 120

STATUS_SCANNED = "scanned"
# Scanning is switched off by setting.
STATUS_DISABLED = "disabled"
# The program is not there (local dev, or an image built without it).
STATUS_UNAVAILABLE = "unavailable"
# The program ran or tried to and produced no usable verdict.
STATUS_FAILED = "failed"

# Findings quote the matched code, which can be long.
_MESSAGE_MAX = 300

# Cap on stderr/stdout quoted in an error so a noisy scanner cannot bloat rows.
_ERROR_TAIL = 500


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    message: str
    file: Optional[str] = None


@dataclass(frozen=True)
class ScanResult:
    status: str
    recommendation: Optional[str] = None  # SAFE | CAUTION | DO_NOT_INSTALL
    score: Optional[int] = None
    max_severity: Optional[str] = None
    findings: List[Finding] = field(default_factory=list)
    scanner_version: Optional[str] = None
    error: Optional[str] = None

    @property
    def scanned(self) -> bool:
        return self.status == STATUS_SCANNED


def skill_digest(folder: Path) -> str:
    """Stable sha256 over every regular file in the skill folder.

    Covers each file's relative posix path and bytes in sorted order, so any
    change to any file, including a rename, changes it. Symlinks are skipped
    and never followed, so a link cannot pull outside content into the hash.
    """
    root = Path(folder)
    files = sorted(
        (p.relative_to(root).as_posix(), p)
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    digest = hashlib.sha256()
    for rel, path in files:
        data = path.read_bytes()
        # Length-prefix both fields so no path/content split can collide.
        digest.update(len(rel.encode()).to_bytes(8, "big") + rel.encode())
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def _child_env(home: str, osv: bool) -> Dict[str, str]:
    # An allowlist, not a scrub of known key names: any model credential or
    # cloud variable the host carries is dropped by default.
    env = {"PATH": os.defpath, "HOME": home, "LANG": "C.UTF-8", "NO_COLOR": "1"}
    if not osv:
        # A zero budget stops the scanner's osv.dev dependency lookup, which
        # would send a skill's package names out; it uses its bundled list.
        env["SKILLSPECTOR_OSV_TIMEOUT"] = "0"
    return env


def _parse(stdout: str) -> ScanResult:
    try:
        report = json.loads(stdout)
        risk = report["risk_assessment"]
        findings = [
            Finding(
                rule_id=str(i.get("id") or ""),
                severity=str(i.get("severity") or ""),
                message=str(i.get("finding") or i.get("pattern") or "")[:_MESSAGE_MAX],
                file=(i.get("location") or {}).get("file"),
            )
            for i in report.get("issues") or []
        ]
        return ScanResult(
            status=STATUS_SCANNED,
            recommendation=str(risk["recommendation"]),
            score=int(risk["score"]),
            max_severity=risk.get("max_issue_severity"),
            findings=findings,
            scanner_version=(report.get("metadata") or {}).get("skillspector_version"),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return ScanResult(STATUS_FAILED, error=f"unparseable scanner output: {exc}")


def scan_skill(
    folder: Path,
    settings: Optional[Settings] = None,
    timeout: float = SCAN_TIMEOUT_SECONDS,
) -> ScanResult:
    """Run SkillSpector over ``folder`` and return its verdict, or why none."""
    settings = settings or get_settings()
    scanner = (settings.vigil_skill_scanner or "").strip()
    if not scanner:
        return ScanResult(STATUS_DISABLED)
    if not (os.path.isfile(scanner) and os.access(scanner, os.X_OK)):
        return ScanResult(STATUS_UNAVAILABLE, error=f"scanner not found: {scanner}")

    # cwd and HOME are a throwaway directory so the scanner has nowhere in the
    # skill folder or the user's home to read from or write to.
    with tempfile.TemporaryDirectory(prefix="skill-scan-") as scratch:
        try:
            proc = subprocess.run(
                [
                    scanner,
                    "scan",
                    str(Path(folder).resolve()),
                    "--no-llm",
                    "--format",
                    "json",
                ],
                cwd=scratch,
                env=_child_env(scratch, settings.vigil_skill_scanner_osv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ScanResult(STATUS_FAILED, error=f"scan timed out after {timeout:g}s")
        except OSError as exc:
            return ScanResult(STATUS_UNAVAILABLE, error=f"scanner not runnable: {exc}")

    # Exit 1 is how the scanner reports a risky skill (it still prints the full
    # report); any other non-zero exit, or exit 1 with no report, is a failure.
    if proc.returncode in (0, 1):
        result = _parse(proc.stdout)
        if result.scanned or proc.returncode == 0:
            return result
    tail = (proc.stderr or proc.stdout or "").strip()[-_ERROR_TAIL:]
    logger.warning("skill scan exited %s for %s", proc.returncode, folder)
    return ScanResult(STATUS_FAILED, error=f"scanner exit {proc.returncode}: {tail}")
