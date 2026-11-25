"""Skills installed from a git repo: fetching them, and where each came from.

An operator names a repo: ``owner/repo`` on GitHub (optionally with a folder,
``owner/repo/skills/x``), a GitHub page link (``.../tree/<ref>/<folder>``) or an
https git URL, with ``@<ref>`` for a branch or tag. Vigil clones it shallowly
into a temporary folder, finds skills where ``npx skills`` looks, and installs
the ones picked as an upload installs them: under the operator root, all or
nothing, nothing overwritten, each scanned and held like any operator skill. Or it
scans the picked ones in the clone without installing anything, and files each
the hold rule holds as a Finding.

A github.com clone carries the stored GitHub token (``GITHUB_TOKEN``, Basic
username ``x-access-token``), so private repos read. Any other allowed DNS host
reads its Basic username and token from ``SKILL_SOURCE_<HOST>_USER`` and
``SKILL_SOURCE_<HOST>_TOKEN`` through ``core.secrets.get_secret`` (encrypted
store, then the environment): ``<HOST>`` is the hostname uppercased, with "."
as ``_D_`` and "-" as ``_H_``, so ``gitlab.example.net`` reads
``SKILL_SOURCE_GITLAB_D_EXAMPLE_D_NET_USER`` and ``..._TOKEN``. Both must be
set; an IP literal gets no credential. A server behind an internal CA is
trusted through ``GIT_SSL_CAINFO`` (or ``SSL_CERT_FILE``/``SSL_CERT_DIR``).

Only hosts in ``vigil_skill_source_hosts`` are fetched from, over https only.
Each installed skill keeps a ``skill_installs`` row: the repo, the commit, and
the digest of its content as installed.

Detection rule libraries (``core.detections.rule_libraries``) read rule repos
through this same reader: ``parse_source``, ``cloned`` and ``remote_commit``,
under the same host list, credentials and caps. Messages on that shared path
say "repo", not "skill".
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Collection, Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from sqlalchemy import delete, select

from core.config import Settings, get_settings
from core.skills.skill_hold import holds, reasons
from core.skills.skill_library import (
    OPERATOR_SKILLS_MAX,
    SKILL_FILE,
    Skill,
    SkillConflict,
    SkillError,
    SkillNotFound,
    delete_operator_skill,
    folder_files,
    inside,
    install_skill_files,
    load_skills,
    parse_skill,
    repo_skill_dirs,
    skill_roots,
    writable_operator_root,
)
from core.skills.skill_scan import (
    STATUS_SCANNED,
    files_digest,
    scan_mode,
    scan_skill,
    skill_digest,
)
from core.storage.models import SkillInstall
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

GITHUB = "github.com"
_SEGMENT = r"[A-Za-z0-9_.-]+"
_SHORTHAND_RE = re.compile(rf"^({_SEGMENT})/({_SEGMENT})((?:/{_SEGMENT})*)/?$")
_SEGMENT_RE = re.compile(rf"^{_SEGMENT}$")
# A branch or tag: never an option to git, never a path climb.
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")
_CLONE_TIMEOUT = 120  # seconds
# A skills repo is a few MB on disk (anthropics/skills: 17 MB with its .git);
# a clone that grows past this is stopped before it can fill the disk.
_CLONE_MAX_BYTES = 200 * 1024 * 1024
_CLONE_POLL_SECONDS = 0.2
# Clones at once in this process; each can use up to _CLONE_MAX_BYTES.
_CLONES = threading.BoundedSemaphore(2)
_SOURCE_MAX = 500


@dataclass(frozen=True)
class RepoSource:
    """A repo to install from, as cloned: never the text the operator typed."""

    url: str  # the https clone URL
    label: str  # how it is shown: owner/repo, or host/path
    host: str
    ref: Optional[str]  # branch or tag; None is the repo's default branch
    path: str  # the folder within the repo to look in; "" is its root


@dataclass(frozen=True)
class Found:
    """A skill folder found in the repo, and why it cannot be installed, if so."""

    folder: str  # posix path within the repo; "" is its root
    name: str
    description: str
    problem: Optional[str] = None
    # The only problem is that a skill by this name is loaded: it can still be
    # scanned without installing, which leaves the library alone.
    installed: bool = False


TAKEN = "a skill with this name is already installed"


def _segments(path: str) -> List[str]:
    parts = [p for p in path.split("/") if p]
    for part in parts:
        if part in (".", "..") or not _SEGMENT_RE.match(part):
            raise SkillError(f"{path!r} is not a folder path Vigil can read")
    return parts


def parse_source(text: str, settings: Optional[Settings] = None) -> RepoSource:
    """The repo ``text`` names, refused unless its host is allowed."""
    settings = settings or get_settings()
    text = (text or "").strip()
    if not text or len(text) > _SOURCE_MAX:
        raise SkillError("Name a repo: owner/repo, or an https link to one")
    ref: Optional[str] = None
    head, at, tail = text.rpartition("@")
    if at and ":" not in tail:
        # A ref may hold "/", so user@host is told apart by where the "@" sits:
        # inside the link's authority it is user info, which is refused below.
        authority_end = text.find("/", text.find("://") + 3) if "://" in text else 0
        if authority_end != -1 and len(head) >= authority_end:
            text, ref = head, tail
    sub: List[str] = []
    guess: List[str] = []  # /tree/ segments: a branch name and then a folder
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme != "https":
            raise SkillError("Only https links are fetched")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise SkillError("The link cannot carry a user, a query or a fragment")
        host = (parts.hostname or "").lower()
        try:
            port = parts.port
        except ValueError:
            port = -1
        if not host or port is not None:
            raise SkillError("The link needs a host, without a port")
        path = _segments(parts.path)
        if host == GITHUB:
            if len(path) < 2:
                raise SkillError("A GitHub link needs an owner and a repo")
            owner, repo, rest = path[0], path[1], path[2:]
            if rest[:1] == ["tree"] and len(rest) >= 2:
                if ref or len(rest) == 2:
                    ref = ref or rest[1]
                    sub = rest[2:]
                else:
                    guess = rest[1:]
            elif rest:
                raise SkillError(
                    "Link to the repo, or to a folder in it (…/tree/<branch>/<folder>)"
                )
            repo = repo.removesuffix(".git")
            url, label = f"https://{GITHUB}/{owner}/{repo}.git", f"{owner}/{repo}"
        else:
            if not path:
                raise SkillError("The link needs a repo path")
            joined = "/".join(path)
            url, label = (
                f"https://{host}/{joined}",
                f"{host}/{joined.removesuffix('.git')}",
            )
    else:
        match = _SHORTHAND_RE.match(text)
        if not match:
            raise SkillError("Name a repo as owner/repo, or paste an https link to one")
        owner, repo = match.group(1), match.group(2).removesuffix(".git")
        _segments(f"{owner}/{repo}")
        sub = _segments(match.group(3))
        host = GITHUB
        url, label = f"https://{GITHUB}/{owner}/{repo}.git", f"{owner}/{repo}"
    if ref is not None and (not _REF_RE.match(ref) or ".." in ref):
        raise SkillError(f"{ref!r} is not a branch or tag name Vigil accepts")
    allowed = [
        h.strip().lower() for h in settings.vigil_skill_source_hosts if h.strip()
    ]
    if not allowed:
        raise SkillError(
            "Reading from a repo is off: no host is allowed "
            "(VIGIL_SKILL_SOURCE_HOSTS)"
        )
    if host not in allowed:
        raise SkillError(
            f"{host} is not an allowed source: an admin can add it to "
            f"VIGIL_SKILL_SOURCE_HOSTS. Allowed: {', '.join(allowed)}"
        )
    if guess:
        # Only now that the host is allowed: the one network call.
        ref, sub = _split_tree(url, host, guess)
    if ref is not None and (not _REF_RE.match(ref) or ".." in ref):
        raise SkillError(f"{ref!r} is not a branch or tag name Vigil accepts")
    return RepoSource(url=url, label=label, host=host, ref=ref, path="/".join(sub))


def _split_tree(url: str, host: str, segments: List[str]) -> tuple[str, List[str]]:
    """Split ``<branch>/<folder>`` at the longest prefix that is a ref on the remote.

    A link cannot say where a branch name holding "/" ends, so, as git does,
    the remote decides: one ``git ls-remote`` for every prefix.
    """
    prefixes = ["/".join(segments[:n]) for n in range(len(segments), 0, -1)]
    prefixes = [p for p in prefixes if _REF_RE.match(p)]
    if not prefixes:
        raise SkillError(f"{'/'.join(segments)!r} is not a branch Vigil accepts")
    patterns = [f"refs/{kind}/{p}" for p in prefixes for kind in ("heads", "tags")]
    scratch = Path(tempfile.mkdtemp(prefix="vigil-skill-repo-"))
    try:
        out = _git(["ls-remote", url, *patterns], scratch, host=host)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    found = set()
    for line in out.splitlines():
        name = line.partition("\t")[2].strip().removesuffix("^{}")
        for kind in ("refs/heads/", "refs/tags/"):
            if name.startswith(kind):
                found.add(name[len(kind) :])
    for prefix in prefixes:  # longest first
        if prefix in found:
            return prefix, segments[len(prefix.split("/")) :]
    raise SkillError("The repo has no branch or tag matching that link")


def _secret(key: str) -> Optional[str]:
    # Imported here: the secrets backend is only needed when a host is read.
    from core.secrets import get_secret

    return (get_secret(key) or "").strip() or None


def _github_token() -> Optional[str]:
    try:
        return _secret("GITHUB_TOKEN")
    except Exception:  # noqa: BLE001 - no token readable: the clone is anonymous
        logger.warning("GitHub token could not be read; cloning anonymously")
        return None


_HOSTNAME_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*$")
_NUMERIC_RE = re.compile(r"^(?:[0-9]+|0x[0-9a-f]*)$")


def _host_keys(host: Optional[str]) -> Optional[tuple[str, str]]:
    """The USER and TOKEN secret names for a non-GitHub DNS host, else None.

    "." is "_D_" and "-" is "_H_", so no two hostnames share a key.
    """
    if not host or host == GITHUB or not _HOSTNAME_RE.match(host):
        return None
    # An IP literal, in any form curl reads (127.1, 2130706433, 0x7f.0.0.1),
    # ends in a numeric label; no DNS name does.
    if _NUMERIC_RE.match(host.rsplit(".", 1)[-1]):
        return None
    stem = "SKILL_SOURCE_" + host.upper().replace(".", "_D_").replace("-", "_H_")
    return f"{stem}_USER", f"{stem}_TOKEN"


def _credential(host: Optional[str]) -> Optional[tuple[str, str]]:
    """The Basic username and token for ``host``, when both are set."""
    if host == GITHUB:
        token = _github_token()
        return ("x-access-token", token) if token else None
    keys = _host_keys(host)
    if not keys:
        return None
    try:
        user, token = _secret(keys[0]), _secret(keys[1])
    except Exception:  # noqa: BLE001 - unreadable: the clone is anonymous
        logger.warning("Credential for %s could not be read; cloning anonymously", host)
        return None
    return (user, token) if user and token else None


def _inherited(name: str) -> bool:
    """An env var the git child must not inherit: a token, or config and
    credential-helper or trace overrides that could leak or redirect it."""
    return (
        name == "GITHUB_TOKEN"
        or name.startswith(("SKILL_SOURCE_", "GIT_CONFIG_", "GIT_TRACE"))
        or name
        in (
            "GIT_CONFIG",
            "GIT_CONFIG_PARAMETERS",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "GIT_CURL_VERBOSE",
            # A credential never goes out over unverified TLS; trust a private
            # CA through GIT_SSL_CAINFO instead.
            "GIT_SSL_NO_VERIFY",
        )
    )


def _git_env(host: Optional[str] = None) -> dict:
    # Keeps proxy and CA variables (GIT_SSL_CAINFO, SSL_CERT_FILE, SSL_CERT_DIR):
    # a self-hosted git server often sits behind an internal CA.
    env = {
        k: v
        for k, v in os.environ.items()  # noqa: ENV001 - child process env
        if not _inherited(k)
    }
    env.update(
        {
            # https only, whatever the host's git config allows.
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_TERMINAL_PROMPT": "0",
            # No system or user config: a url.<x>.insteadOf there could send the
            # clone to a host the allowed list does not hold.
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    cred = _credential(host)
    if cred:
        # Through the environment only, scoped to the exact host: never argv,
        # the URL, a log line or a message.
        basic = base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
    return env


def _size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass  # git renames its temp files as it goes
    return total


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# What git prints when the host would not let it read the repo.
_REFUSED = (
    "could not read Username",
    "Repository not found",
    "Authentication failed",
    "Invalid username or token",
    "HTTP Basic: Access denied",  # GitLab
    "The requested URL returned error: 401",
    "The requested URL returned error: 403",
)
# Transport failures, told apart without echoing stderr from a credentialed call.
_TRANSPORT = (
    (
        ("SSL certificate problem", "server certificate verification failed"),
        "the TLS certificate of {host} is not trusted",
    ),
    (
        (
            "Could not resolve host",
            "Failed to connect",
            "Connection refused",
            "Connection timed out",
            "Operation timed out",
        ),
        "could not connect to {host}",
    ),
)


def _git(
    args: List[str],
    scratch: Path,
    cwd: Optional[Path] = None,
    host: Optional[str] = None,
) -> str:
    """Run git; stop it past the time limit or once ``scratch`` outgrows the cap.

    ``scratch`` holds the clone and git's output files, so its size is what
    the clone has written. A call to a host with a stored credential carries
    it; if the host refuses it (expired, or a placeholder), the call is tried
    once without, so a bad token does not stop public repos from reading.
    """
    env = _git_env(host)
    tokened = "GIT_CONFIG_COUNT" in env
    code, err = _run_git(args, scratch, cwd, env)
    refused = code != 0 and any(r in err for r in _REFUSED)
    if refused and tokened:
        code, err = _run_git(args, scratch, cwd, _git_env())
        refused = code != 0 and any(r in err for r in _REFUSED)
        if code == 0:
            logger.warning(
                "%s refused the stored repo credential; read anonymously",
                host,
            )
    if code != 0:
        if refused:
            raise SkillError(_refusal(host, tokened))
        if "Remote branch" in err and "not found" in err:
            raise SkillError("The repo has no branch or tag by that name")
        for marks, message in _TRANSPORT:
            if any(m in err for m in marks):
                raise SkillError(
                    "could not fetch the repo: " + message.format(host=host)
                )
        lines = [line for line in err.strip().splitlines() if line.strip()]
        # Raw stderr only from an anonymous call: nothing secret was in play.
        last = lines[-1] if lines and not tokened else "git failed"
        raise SkillError(f"could not fetch the repo: {last}")
    return (scratch / ".git-out").read_text(errors="replace").strip()


def _refusal(host: Optional[str], tokened: bool) -> str:
    """Why a refused read failed, naming what the operator can set."""
    if host == GITHUB:
        return (
            "No repo by that name that Vigil can read: the GitHub token set "
            "(Settings → Integrations → GitHub) was refused or cannot read it"
            if tokened
            else "No repo by that name that Vigil can read. For a private "
            "github.com repo, set a GitHub token (Settings → Integrations → GitHub)"
        )
    if tokened:
        return (
            f"The stored credential for {host} did not grant repository "
            "access. Check token expiry and read permission."
        )
    keys = _host_keys(host)
    if keys:
        return f"No credential for {host} is stored. Set {keys[0]} and {keys[1]}."
    return "No repo by that name that Vigil can read"


def _run_git(
    args: List[str], scratch: Path, cwd: Optional[Path], env: dict
) -> tuple[int, str]:
    """One git run: its exit code and stderr; raises past the time or size cap."""
    out_path, err_path = scratch / ".git-out", scratch / ".git-err"
    with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
        try:
            proc = subprocess.Popen(
                [
                    "git",
                    # A symlink in the repo is checked out as a plain file, so
                    # nothing read from the clone can point outside it.
                    "-c",
                    "core.symlinks=false",
                    # A redirect could lead to a host the allowed list does not hold.
                    "-c",
                    "http.followRedirects=false",
                    *args,
                ],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                # Its own process group, so a stop takes git's helpers with it.
                start_new_session=True,
            )
        except OSError as exc:
            raise SkillError(f"git could not run: {exc.strerror}") from exc
        deadline = time.monotonic() + _CLONE_TIMEOUT
        problem: Optional[str] = None
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                problem = f"the repo took longer than {_CLONE_TIMEOUT}s to fetch"
            elif _size(scratch) > _CLONE_MAX_BYTES:
                problem = (
                    f"the repo is larger than {_CLONE_MAX_BYTES // (1024 * 1024)} MB"
                )
            if problem:
                break
            time.sleep(_CLONE_POLL_SECONDS)
        # Also on a normal exit: no helper may outlive the call.
        _kill_group(proc)
        proc.wait()
    if problem:
        raise SkillError(problem)
    return proc.returncode, err_path.read_text(errors="replace")


@contextmanager
def cloned(source: RepoSource) -> Iterator[tuple[Path, str]]:
    """A shallow clone of ``source`` in a temporary folder, and its commit."""
    if not _CLONES.acquire(blocking=False):
        raise SkillError("Vigil is already reading other repos; try again shortly")
    scratch = Path(tempfile.mkdtemp(prefix="vigil-skill-repo-"))
    try:
        repo = scratch / "repo"
        branch = ["--branch", source.ref] if source.ref else []
        _git(
            [
                "clone",
                "--quiet",
                "--depth",
                "1",
                "--single-branch",
                "--no-tags",
                *branch,
            ]
            + ["--", source.url, str(repo)],
            scratch,
            host=source.host,
        )
        yield repo, _git(["rev-parse", "HEAD"], scratch, cwd=repo)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        _CLONES.release()


def discover(repo: Path, source: RepoSource, own: Collection[str] = ()) -> List[Found]:
    """The skill folders in the clone, as ``npx skills`` finds them.

    Each that cannot be installed carries why: it does not parse, another
    folder has its name, or a skill by that name is already loaded (other than
    one of ``own``, the skills installed from this repo already).
    """
    top = repo if not source.path else inside(repo, f"./{source.path}")
    if top is None:
        raise SkillError(f"{source.path!r} is not a folder in the repo")
    dirs = [top] if (top / SKILL_FILE).is_file() else repo_skill_dirs(top)
    if not dirs:
        raise SkillError(f"no {SKILL_FILE} found where skills are looked for")
    taken = {skill.name for skill in load_skills(skill_roots())} - set(own)
    found: List[Found] = []
    names: set[str] = set()
    for folder in dirs:
        rel = folder.relative_to(repo).as_posix()
        rel = "" if rel == "." else rel
        try:
            skill = parse_skill(folder, match_dir=folder != repo)
        except SkillError as exc:
            found.append(Found(rel, folder.name if rel else source.label, "", str(exc)))
            continue
        problem = None
        if skill.name in names:
            problem = "another folder in the repo has this name"
        else:
            try:
                folder_files(folder)  # over the size or file limit: say so up front
            except SkillError as exc:
                problem = str(exc)
        installed = problem is None and skill.name in taken
        names.add(skill.name)
        found.append(
            Found(
                rel,
                skill.name,
                skill.description,
                TAKEN if installed else problem,
                installed,
            )
        )
    return found


def _by_name(found: List[Found]) -> Dict[str, Found]:
    # Two folders can carry one name; the one discover passed is the one shown.
    # An installed-only clash still scans, so it ranks above a refused duplicate.
    def rank(f: Found) -> int:
        return 0 if not f.problem else 1 if f.installed else 2

    named: Dict[str, Found] = {}
    for f in found:
        if f.name not in named or rank(f) < rank(named[f.name]):
            named[f.name] = f
    return named


def preview(
    text: str, settings: Optional[Settings] = None
) -> tuple[RepoSource, str, List[Found]]:
    """What installing from ``text`` would offer: the source, its commit, its skills."""
    source = parse_source(text, settings)
    with cloned(source) as (repo, commit):
        return source, commit, discover(repo, source)


def install_from_repo(
    text: str,
    commit: str,
    names: List[str],
    installed_by: Optional[str],
    settings: Optional[Settings] = None,
) -> List[Skill]:
    """Install the skills ``names`` from the repo ``text`` at ``commit``.

    ``commit`` is the one the operator was shown: a repo that has moved since is
    refused rather than installing content nobody looked at.
    """
    if not names:
        raise SkillError("Pick at least one skill to install")
    if len(set(names)) != len(names) or len(names) > OPERATOR_SKILLS_MAX:
        raise SkillError("Each skill can be picked once")
    source = parse_source(text, settings)
    root = writable_operator_root(settings)
    with cloned(source) as (repo, got):
        if got != commit:
            raise SkillConflict(
                f"{source.label} has changed since it was read (now {got[:7]}). "
                "Read it again before installing."
            )
        found = _by_name(discover(repo, source))
        for name in names:
            if name not in found:
                raise SkillError(f"{name!r} is not a skill in {source.label}")
            if found[name].problem:
                raise SkillConflict(f"{name!r}: {found[name].problem}")
        named = [(name, folder_files(repo / found[name].folder)) for name in names]
        skills = install_skill_files(root, named)
    try:
        _record(
            source, commit, skills, {n: found[n].folder for n in names}, installed_by
        )
    except Exception:  # noqa: BLE001 - installed; only where it came from is lost
        logger.exception("skills from %s installed but not recorded", source.label)
    return skills


# ---- Scanning a repo's skills without installing them ----

SCAN_SOURCE = "skill-scan"  # the data_source of the Findings a scan raises
# Scans at once in one request; each is its own scanner process.
_SCAN_WORKERS = 4
_SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}
NOT_SCANNED = "not_scanned"


@dataclass(frozen=True)
class Scanned:
    """One picked skill's scan: its verdict and reasons, or why there is none."""

    name: str
    folder: str
    # scanned | failed | unavailable | disabled | not_scanned
    status: str
    held: Optional[bool] = None  # whether the hold rule holds it; None: no verdict
    recommendation: Optional[str] = None
    score: Optional[int] = None
    max_severity: Optional[str] = None
    reasons: List[dict] = field(default_factory=list)
    error: Optional[str] = None
    finding_id: Optional[str] = None
    finding_new: bool = False  # False with an id: a rescan found it already raised


def _severity(max_severity: Optional[str], recommendation: Optional[str]) -> str:
    """The scan's max severity in the Finding vocabulary."""
    sev = (max_severity or "").upper()
    if sev in _SEVERITIES:
        return sev.lower()
    return "high" if recommendation == "DO_NOT_INSTALL" else "medium"


def _raise_finding(
    service, source: RepoSource, commit: str, scanned: Scanned, digest: str, mode: str
) -> Scanned:
    """File the held skill as a Finding; a rescan of the same content finds the old one."""
    from core.ingestion.ingestion_service import _content_finding_id
    from core.time import utcnow

    external_id = hashlib.sha256(
        f"{source.url}\n{scanned.name}\n{digest}".encode()
    ).hexdigest()
    lines = [
        f"The skill scan ({mode}) holds skill {scanned.name!r} in {source.label}.",
        f"Repository: {source.url}",
        f"Commit: {commit}",
        f"Folder: {scanned.folder or '(repo root)'}",
        f"Recommendation: {scanned.recommendation or 'none'}"
        + (f" (score {scanned.score})" if scanned.score is not None else ""),
        "",
        "Reasons:",
        *(
            f"- [{r['severity']}] {r['rule_id']}"
            + (f" in {r['file']}" if r.get("file") else "")
            + f": {r['message']}"
            for r in scanned.reasons
        ),
    ]
    if not scanned.reasons:
        lines.append("- none listed; the recommendation alone holds it")
    if service.db_service is None:
        return replace(scanned, error="the Finding could not be saved")
    before = service.stats["findings_imported"]
    service.ingest_finding(
        {
            "finding_id": _content_finding_id(f"{SCAN_SOURCE}:{external_id}"),
            "external_id": external_id,
            "data_source": SCAN_SOURCE,
            "timestamp": utcnow().isoformat(),
            "severity": _severity(scanned.max_severity, scanned.recommendation),
            "title": f"Skill {scanned.name} in {source.label} held by the skill scan",
            "description": "\n".join(lines),
            "entity_context": {"urls": [source.url]},
        }
    )
    try:
        row = service.db_service.get_finding_by_external_id(SCAN_SOURCE, external_id)
    except Exception:  # noqa: BLE001 - reported per skill below
        logger.exception("could not read back the Finding for %s", scanned.name)
        row = None
    if row is None:
        return replace(scanned, error="the Finding could not be saved")
    return replace(
        scanned,
        finding_id=row.finding_id,
        finding_new=service.stats["findings_imported"] > before,
    )


def scan_repo(
    text: str,
    commit: str,
    names: List[str],
    settings: Optional[Settings] = None,
) -> tuple[RepoSource, str, List[Scanned]]:
    """Scan the skills ``names`` in the repo ``text`` at ``commit``, installing nothing.

    Each is scanned in the clone, before it is deleted. Nothing is written under
    the operator root or to ``skill_scans``: those are the installed library's.
    A skill the hold rule holds becomes a Finding; a scan with no verdict is
    reported and is not one. A name already installed can still be scanned.
    """
    if not names:
        raise SkillError("Pick at least one skill to scan")
    if len(set(names)) != len(names) or len(names) > OPERATOR_SKILLS_MAX:
        raise SkillError("Each skill can be picked once")
    settings = settings or get_settings()
    mode = scan_mode(settings)
    source = parse_source(text, settings)
    out: Dict[str, Scanned] = {}
    digests: Dict[str, str] = {}
    with cloned(source) as (repo, got):
        if got != commit:
            raise SkillConflict(
                f"{source.label} has changed since it was read (now {got[:7]}). "
                "Read it again before scanning."
            )
        found = _by_name(discover(repo, source))
        for name in names:
            if name not in found:
                raise SkillError(f"{name!r} is not a skill in {source.label}")
        todo = []
        for name in names:
            f = found[name]
            if f.problem and not f.installed:
                out[name] = Scanned(name, f.folder, NOT_SCANNED, error=f.problem)
            else:
                todo.append(name)

        def one(name: str) -> Scanned:
            folder = repo / found[name].folder
            try:
                digests[name] = skill_digest(folder)
            except OSError as exc:  # one unreadable skill must not sink the rest
                return Scanned(name, found[name].folder, NOT_SCANNED, error=str(exc))
            result = scan_skill(folder, settings)
            if result.status != STATUS_SCANNED:
                return Scanned(
                    name, found[name].folder, result.status, error=result.error
                )
            why = reasons(result)
            return Scanned(
                name,
                found[name].folder,
                result.status,
                held=holds(result.recommendation, result.max_severity, why, mode),
                recommendation=result.recommendation,
                score=result.score,
                max_severity=result.max_severity,
                reasons=why,
            )

        if todo:
            with ThreadPoolExecutor(max_workers=min(_SCAN_WORKERS, len(todo))) as pool:
                out.update(zip(todo, pool.map(one, todo)))
    held = [n for n in names if out[n].held]
    if held:
        from core.ingestion.ingestion_service import IngestionService

        service = IngestionService()
        for name in held:
            out[name] = _raise_finding(
                service, source, commit, out[name], digests[name], mode
            )
    return source, commit, [out[n] for n in names]


def _record(
    source: RepoSource,
    commit: str,
    skills: List[Skill],
    folders: dict,
    installed_by: Optional[str],
    drop: Collection[str] = (),
) -> None:
    rows = [
        SkillInstall(
            skill_name=skill.name,
            repo_url=source.url,
            label=source.label,
            ref=source.ref,
            path=source.path,
            folder=folders[skill.name],
            commit_sha=commit,
            digest=skill_digest(skill.path),
            installed_by=installed_by,
        )
        for skill in skills
    ]
    with unit_of_work() as session:
        session.execute(
            delete(SkillInstall).where(
                SkillInstall.skill_name.in_([s.name for s in skills] + list(drop))
            )
        )
        session.add_all(rows)


def forget(name: str) -> None:
    """Drop where a deleted skill came from; a later skill by that name is new."""
    with unit_of_work() as session:
        session.execute(delete(SkillInstall).where(SkillInstall.skill_name == name))


# ---- A library: the skills installed from one repo, ref and folder ----


@dataclass(frozen=True)
class Change:
    """A skill an update would change, add or remove, and what it would do."""

    name: str
    folder: str
    note: str  # what changed, the description of a new one, or "" when gone
    edited: bool = False  # edited in Vigil since it was installed
    problem: Optional[str] = None


@dataclass(frozen=True)
class LibraryPreview:
    source: RepoSource
    commit: str  # the commit the asked-about skill was installed from
    latest: str
    changed: List[Change]
    new: List[Change]
    gone: List[Change]


def sources(names: Collection[str]) -> Dict[str, dict]:
    """Where each of ``names`` came from, for those installed from a repo."""
    wanted = set(names)
    with unit_of_work() as session:
        every = [
            (r.skill_name, r.repo_url, r.label, r.ref, r.path, r.commit_sha)
            for r in session.scalars(select(SkillInstall))
        ]
    # Counted over every row, so one skill's detail still says how many came with it.
    counts = Counter((url, ref, path) for _, url, _, ref, path, _ in every)
    rows = [row for row in every if row[0] in wanted]
    return {
        name: {
            "label": label,
            "ref": ref,
            "path": path,
            "commit": commit,
            "skills": counts[(url, ref, path)],
        }
        for name, url, label, ref, path, commit in rows
    }


def _library(name: str) -> tuple[SkillInstall, List[SkillInstall]]:
    """The install row of ``name`` and every row of its library."""
    with unit_of_work() as session:
        row = session.get(SkillInstall, name)
        if row is None:
            raise SkillNotFound(f"{name!r} was not installed from a repo")
        rows = list(
            session.scalars(
                select(SkillInstall).where(
                    SkillInstall.repo_url == row.repo_url,
                    (
                        SkillInstall.ref.is_(None)
                        if row.ref is None
                        else SkillInstall.ref == row.ref
                    ),
                    SkillInstall.path == row.path,
                )
            )
        )
        session.expunge_all()
    return row, rows


def _row_source(row: SkillInstall, settings: Optional[Settings]) -> RepoSource:
    """The row's repo, checked again: the allowed hosts may have changed."""
    at = f"@{row.ref}" if row.ref else ""
    source = parse_source(f"{row.repo_url}{at}", settings)
    return replace(source, path="/".join(_segments(row.path)))


def latest_commit(name: str, settings: Optional[Settings] = None) -> Optional[str]:
    """The commit ``name``'s repo is at now, or None when its ref is gone.

    One ``git ls-remote``: nothing is cloned.
    """
    row, _ = _library(name)
    return remote_commit(_row_source(row, settings))


def remote_commit(source: RepoSource) -> Optional[str]:
    """The commit ``source``'s ref (or default branch) is at now, or None when the
    ref is gone. One ``git ls-remote``: nothing is cloned."""
    scratch = Path(tempfile.mkdtemp(prefix="vigil-skill-repo-"))
    try:
        out = _git(
            ["ls-remote", source.url, source.ref or "HEAD"], scratch, host=source.host
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    refs = {}
    for line in out.splitlines():
        sha, _, refname = line.partition("\t")
        refs[refname.strip()] = sha.strip()
    if not source.ref:
        return refs.get("HEAD")
    # A clone of a tag checks out the commit it points to: the peeled ^{} line.
    for key in (
        f"refs/heads/{source.ref}",
        f"refs/tags/{source.ref}^{{}}",
        f"refs/tags/{source.ref}",
    ):
        if key in refs:
            return refs[key]
    return None


def _summary(here: Dict[str, bytes], there: Dict[str, bytes]) -> str:
    """What taking ``there`` does to the copy ``here``, in a few words."""
    changed = [p for p in there if p in here and here[p] != there[p]]
    parts = []
    if SKILL_FILE in changed:
        changed.remove(SKILL_FILE)
        parts.append(f"{SKILL_FILE} changed")
    for count, word in (
        (len(changed), "changed"),
        (len([p for p in there if p not in here]), "added"),
        (len([p for p in here if p not in there]), "removed"),
    ):
        if count:
            parts.append(f"{count} {'file' if count == 1 else 'files'} {word}")
    return ", ".join(parts) or "Same as your copy"


@dataclass(frozen=True)
class _Diff:
    changed: List[Change]
    new: List[Change]
    gone: List[Change]
    unchanged: List[str]  # members whose repo copy is what was installed


def _edited(skill: Skill, row: SkillInstall) -> bool:
    return skill_digest(skill.path) != row.digest


def _diff(repo: Path, source: RepoSource, rows: List[SkillInstall]) -> _Diff:
    """The library's skills the clone changed, added and dropped.

    A skill counts as changed when the repo's copy differs from what was
    installed; ``edited`` says the copy here differs from that too, so taking
    the update (or removing one gone from the repo) would lose edits made in
    Vigil.
    """
    loaded = {skill.name: skill for skill in load_skills(skill_roots())}
    # A row whose skill is gone from disk is not a member: it shows as new.
    mine = {row.skill_name: row for row in rows if row.skill_name in loaded}
    try:
        found = _by_name(discover(repo, source, own=mine))
    except SkillError:
        found = {}  # the folder, or every skill in it, left the repo
    changed: List[Change] = []
    gone: List[Change] = []
    unchanged: List[str] = []
    for name, row in sorted(mine.items()):
        there = found.get(name)
        if there is None:
            gone.append(Change(name, row.folder, "", _edited(loaded[name], row)))
            continue
        if there.problem:
            changed.append(Change(name, there.folder, "", problem=there.problem))
            continue
        files = folder_files(repo / there.folder)
        if files_digest(files) == row.digest:
            unchanged.append(name)
            continue
        try:
            here = folder_files(loaded[name].path)
        except SkillError:
            here = {}
        changed.append(
            Change(
                name,
                there.folder,
                _summary(here, files),
                _edited(loaded[name], row),
            )
        )
    new = [
        Change(f.name, f.folder, f.description, problem=f.problem)
        for f in found.values()
        if f.name not in mine
    ]
    return _Diff(changed, sorted(new, key=lambda c: c.name), gone, unchanged)


def _advance(names: Collection[str], commit: str) -> None:
    """Mark skills the repo left as they were as current at ``commit``."""
    if not names:
        return
    with unit_of_work() as session:
        for row in session.scalars(
            select(SkillInstall).where(SkillInstall.skill_name.in_(list(names)))
        ):
            row.commit_sha = commit


def library_preview(name: str, settings: Optional[Settings] = None) -> LibraryPreview:
    """What updating ``name``'s library would offer.

    Only the record of a skill the repo left as it was changes: it is current
    at the commit read, so its panel stops offering a new version.
    """
    row, rows = _library(name)
    source = _row_source(row, settings)
    with cloned(source) as (repo, latest):
        diff = _diff(repo, source, rows)
    try:
        _advance(diff.unchanged, latest)
    except Exception:  # noqa: BLE001 - only the "new version" hint is stale
        logger.exception("could not advance unchanged skills of %s", source.label)
    commit = latest if name in diff.unchanged else row.commit_sha
    return LibraryPreview(source, commit, latest, diff.changed, diff.new, diff.gone)


def update_library(
    name: str,
    commit: str,
    take: List[str],
    installed_by: Optional[str],
    edited: Collection[str] = (),
    settings: Optional[Settings] = None,
) -> tuple[List[Skill], List[str]]:
    """Take the picked changes to ``name``'s library from the repo at ``commit``.

    A changed skill is replaced where it lives, a new one installed, a gone one
    deleted. The installs are all or nothing, as an upload's are; each skill
    taken is scanned and held again. A pick edited in Vigil must be in
    ``edited``, the ones the operator was warned about, or nothing is taken.
    Returns the skills written and the names deleted.
    """
    if not take:
        raise SkillError("Pick at least one skill to update")
    if len(set(take)) != len(take) or len(take) > OPERATOR_SKILLS_MAX:
        raise SkillError("Each skill can be picked once")
    row, rows = _library(name)
    source = _row_source(row, settings)
    root = writable_operator_root(settings)
    with cloned(source) as (repo, got):
        if got != commit:
            raise SkillConflict(
                f"{source.label} has changed since it was read (now {got[:7]}). "
                "Read it again before updating."
            )
        diff = _diff(repo, source, rows)
        offered = {
            c.name: (kind, c)
            for kind, group in (
                ("changed", diff.changed),
                ("new", diff.new),
                ("gone", diff.gone),
            )
            for c in group
        }
        for pick in take:
            if pick not in offered:
                raise SkillConflict(f"{pick!r} has nothing to update; read it again")
            change = offered[pick][1]
            if change.problem:
                raise SkillConflict(f"{pick!r}: {change.problem}")
            if change.edited and pick not in edited:
                raise SkillConflict(
                    f"{pick!r} was edited here since the repo was read; read it again"
                )
        put = [p for p in take if offered[p][0] != "gone"]
        named = [(p, folder_files(repo / offered[p][1].folder)) for p in put]
        replacing = {p for p in put if offered[p][0] == "changed"}
        gone = sum(1 for p in take if offered[p][0] == "gone")
        skills = install_skill_files(root, named, replacing, gone) if named else []
    removed: List[str] = []
    try:
        for pick in take:
            if offered[pick][0] != "gone":
                continue
            try:
                delete_operator_skill(pick, settings)
            except SkillNotFound:
                pass  # already gone; only its record is left to drop
            removed.append(pick)
    finally:
        # Also when a delete failed: what was written must still be recorded.
        try:
            _record(
                source,
                commit,
                skills,
                {p: offered[p][1].folder for p in put},
                installed_by,
                drop=removed,
            )
            _advance(diff.unchanged, commit)
        except Exception:  # noqa: BLE001 - written; only where it came from is stale
            logger.exception("library %s updated but not recorded", source.label)
    return skills, removed


def delete_library(name: str, settings: Optional[Settings] = None) -> List[str]:
    """Delete every skill installed from ``name``'s library, and stop tracking it."""
    _, rows = _library(name)
    loaded = {skill.name for skill in load_skills(skill_roots())}
    deleted: List[str] = []
    done: List[str] = []  # rows to drop, a skill already gone from disk too
    try:
        for row in rows:
            if row.skill_name in loaded:
                delete_operator_skill(row.skill_name, settings)
                deleted.append(row.skill_name)
            done.append(row.skill_name)
    finally:
        with unit_of_work() as session:
            session.execute(
                delete(SkillInstall).where(SkillInstall.skill_name.in_(done))
            )
    return deleted
