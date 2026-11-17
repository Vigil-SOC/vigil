"""Scan a skill folder with NVIDIA SkillSpector, run as a separate program.

SkillSpector lives in its own virtualenv and is invoked as a subprocess with a
time limit, never imported: its dependency tree must not mix with the backend's,
and a scan of untrusted content should not run inside the API process. Only the
static analyzers run (``--no-llm``) unless ``vigil_skill_scanner_model`` names a
Bifrost model; then the model-backed analyzers run too, reaching only Bifrost.
The child gets a minimal environment either way, so it cannot call a model the
host did not choose. Its one other network use, the osv.dev dependency lookup,
is off unless a setting turns it on.

``scan_skill`` never raises. It returns a ``ScanResult`` whose ``status`` says
whether a verdict exists; only ``scanned`` carries one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

SCAN_TIMEOUT_SECONDS = 120
# Model calls dominate a model-backed scan (about a minute, and over two when
# the provider rate-limits), so it gets room to retry before it counts as hung.
LLM_SCAN_TIMEOUT_SECONDS = 600

MODE_STATIC = "static"

STATUS_SCANNED = "scanned"
# Scanning is switched off by setting.
STATUS_DISABLED = "disabled"
# The program is not there (local dev, or an image built without it).
STATUS_UNAVAILABLE = "unavailable"
# The program ran or tried to and produced no usable verdict.
STATUS_FAILED = "failed"

# Findings quote the matched code, which can be long.
_MESSAGE_MAX = 300

# A report is small; a hostile skill must not be able to make the scanner fill
# the disk or the API's memory by printing without end.
_OUTPUT_MAX_BYTES = 10 * 1024 * 1024
_POLL_SECONDS = 0.1

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
    # How the verdict was reached: ``static`` or ``llm:<model>``.
    mode: str = MODE_STATIC

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


def files_digest(files: Dict[str, bytes]) -> str:
    """``skill_digest`` of a folder holding exactly ``files`` ({posix path: bytes})."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        digest.update(len(rel.encode()).to_bytes(8, "big") + rel.encode())
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def scan_mode(settings: Optional[Settings] = None) -> str:
    """``static``, or ``llm:<model>`` when a scanner model is configured."""
    model = ((settings or get_settings()).vigil_skill_scanner_model or "").strip()
    return f"llm:{model}" if model else MODE_STATIC


def _llm_env(settings: Settings, model: str) -> Dict[str, str]:
    # Imported here: the router pulls in the whole LLM stack, which static
    # scanning and the hold's read path never need.
    from core.llm.router.router import bifrost_headers

    # Same virtual key as the rest of vigil's model calls, so the spend is
    # budgeted and visible there. bifrost_headers fails open, like every caller.
    headers = {k: v for k, v in bifrost_headers().items() if k == "x-bf-vk"}
    env = {
        "SKILLSPECTOR_PROVIDER": "openai_compatible",
        "SKILLSPECTOR_COMPAT_BASE_URL": f"{settings.bifrost_url.rstrip('/')}/v1",
        # Bifrost inference takes no credential; the client wants a non-empty one.
        "SKILLSPECTOR_COMPAT_API_KEY": "bifrost",
        "SKILLSPECTOR_MODEL": model,
    }
    if headers:
        env["SKILLSPECTOR_COMPAT_HEADERS"] = json.dumps(headers)
    return env


def _child_env(
    home: str, osv: bool, llm: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    # An allowlist, not a scrub of known key names: any model credential or
    # cloud variable the host carries is dropped by default.
    env = {"PATH": os.defpath, "HOME": home, "LANG": "C.UTF-8", "NO_COLOR": "1"}
    if not osv:
        # A zero budget stops the scanner's osv.dev dependency lookup, which
        # would send a skill's package names out; it uses its bundled list.
        env["SKILLSPECTOR_OSV_TIMEOUT"] = "0"
    if llm:
        env.update(llm)
    return env


def _llm_incomplete(report: dict) -> Optional[str]:
    """Why a model-backed report cannot be trusted, or None when it ran fully.

    Without the model the report is only static findings, which under the
    model-mode rule would read as safe. A call that failed after its retries
    leaves an analyzer blind the same way.
    """
    meta = report.get("metadata") or {}
    if not meta.get("llm_available"):
        return "the model was not available to the scanner"
    attempted = int(meta.get("llm_calls_attempted") or 0)
    succeeded = int(meta.get("llm_calls_succeeded") or 0)
    if attempted == 0:
        return "the scanner made no model calls"
    if succeeded < attempted:
        return f"{attempted - succeeded} of {attempted} model calls failed"
    statuses = (report.get("analysis_completeness") or {}).get("analyzer_statuses")
    failed = [s.get("analyzer_id") for s in statuses or [] if s.get("failed")]
    if failed:
        return f"analyzers failed: {', '.join(map(str, failed))}"
    return None


def _parse(stdout: str, mode: str = MODE_STATIC) -> ScanResult:
    try:
        report = json.loads(stdout)
        if mode != MODE_STATIC:
            incomplete = _llm_incomplete(report)
            if incomplete:
                return ScanResult(
                    STATUS_FAILED,
                    error=f"model scan incomplete: {incomplete}",
                    mode=mode,
                )
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
            mode=mode,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return ScanResult(
            STATUS_FAILED, error=f"unparseable scanner output: {exc}", mode=mode
        )


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _oversize(out_path: Path, err_path: Path) -> Optional[str]:
    if max(out_path.stat().st_size, err_path.stat().st_size) > _OUTPUT_MAX_BYTES:
        return f"scanner output exceeded {_OUTPUT_MAX_BYTES // (1024 * 1024)} MB"
    return None


def _wait(
    proc: "subprocess.Popen[bytes]", out_path: Path, err_path: Path, timeout: float
) -> Optional[str]:
    """Wait for the scanner; return why to give up, or None when it exited."""
    deadline = time.monotonic() + timeout
    problem: Optional[str] = None
    while proc.poll() is None:
        if time.monotonic() >= deadline:
            problem = f"scan timed out after {timeout:g}s"
        else:
            problem = _oversize(out_path, err_path)
        if problem:
            break
        time.sleep(_POLL_SECONDS)
    # Also on a normal exit: a descendant left running must not outlive the scan.
    _kill_group(proc)
    proc.wait()
    return problem or _oversize(out_path, err_path)


def scan_skill(
    folder: Path,
    settings: Optional[Settings] = None,
    timeout: Optional[float] = None,
) -> ScanResult:
    """Run SkillSpector over ``folder`` and return its verdict, or why none."""
    settings = settings or get_settings()
    mode = scan_mode(settings)
    model = mode.partition(":")[2]
    if timeout is None:
        timeout = LLM_SCAN_TIMEOUT_SECONDS if model else SCAN_TIMEOUT_SECONDS
    scanner = (settings.vigil_skill_scanner or "").strip()
    if not scanner:
        return ScanResult(STATUS_DISABLED, mode=mode)
    if not (os.path.isfile(scanner) and os.access(scanner, os.X_OK)):
        return ScanResult(
            STATUS_UNAVAILABLE, error=f"scanner not found: {scanner}", mode=mode
        )
    llm_env = _llm_env(settings, model) if model else None

    # cwd and HOME are a throwaway directory so the scanner has nowhere in the
    # skill folder or the user's home to read from or write to.
    with tempfile.TemporaryDirectory(prefix="skill-scan-") as scratch:
        out_path = Path(scratch) / "stdout.txt"
        err_path = Path(scratch) / "stderr.txt"
        # Output goes to files, not pipes: a grandchild that inherits a pipe
        # would keep a read open after the scanner is killed and block us.
        with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
            try:
                proc = subprocess.Popen(
                    [
                        scanner,
                        "scan",
                        str(Path(folder).resolve()),
                        *([] if model else ["--no-llm"]),
                        "--format",
                        "json",
                    ],
                    cwd=scratch,
                    env=_child_env(scratch, settings.vigil_skill_scanner_osv, llm_env),
                    stdin=subprocess.DEVNULL,
                    stdout=out_f,
                    stderr=err_f,
                    # Its own process group, so a timeout can kill the scanner
                    # and everything it spawned, not just the direct child.
                    start_new_session=True,
                )
            except OSError as exc:
                return ScanResult(
                    STATUS_UNAVAILABLE, error=f"scanner not runnable: {exc}", mode=mode
                )
            problem = _wait(proc, out_path, err_path, timeout)
        if problem:
            return ScanResult(STATUS_FAILED, error=problem, mode=mode)
        stdout = out_path.read_text(errors="replace")
        stderr = err_path.read_text(errors="replace")

    # Exit 1 is how the scanner reports a risky skill (it still prints the full
    # report); any other non-zero exit, or exit 1 with no report, is a failure.
    if proc.returncode in (0, 1):
        result = _parse(stdout, mode)
        if result.scanned or proc.returncode == 0:
            return result
    tail = (stderr or stdout or "").strip()[-_ERROR_TAIL:]
    logger.warning("skill scan exited %s for %s", proc.returncode, folder)
    return ScanResult(
        STATUS_FAILED, error=f"scanner exit {proc.returncode}: {tail}", mode=mode
    )
