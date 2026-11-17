"""Skills installed from a git repo: fetching them, and where each came from.

An operator names a repo: ``owner/repo`` on GitHub (optionally with a folder,
``owner/repo/skills/x``), a GitHub page link (``.../tree/<ref>/<folder>``) or an
https git URL, with ``@<ref>`` for a branch or tag. Vigil clones it shallowly
into a temporary folder, finds skills where ``npx skills`` looks, and installs
the ones picked as an upload installs them: under the operator root, all or
nothing, nothing overwritten, each scanned and held like any operator skill.

Only hosts in ``vigil_skill_source_hosts`` are fetched from, over https only.
Each installed skill keeps a ``skill_installs`` row: the repo, the commit, and
the digest of its content as installed.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlsplit

from sqlalchemy import delete

from core.config import Settings, get_settings
from core.skills.skill_library import (
    OPERATOR_SKILLS_MAX,
    SKILL_FILE,
    Skill,
    SkillConflict,
    SkillError,
    folder_files,
    inside,
    install_skill_files,
    load_skills,
    parse_skill,
    repo_skill_dirs,
    skill_roots,
    writable_operator_root,
)
from core.skills.skill_scan import skill_digest
from core.storage.models import SkillInstall
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

GITHUB = "github.com"
_SEGMENT = r"[A-Za-z0-9_.-]+"
_SHORTHAND_RE = re.compile(rf"^({_SEGMENT})/({_SEGMENT})((?:/{_SEGMENT})*)/?$")
_SEGMENT_RE = re.compile(rf"^{_SEGMENT}$")
# A branch or tag: never an option to git, never a path climb.
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")
_CLONE_TIMEOUT = 120  # seconds; bounds how much a huge repo can download
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
    if at and "/" not in tail and ":" not in tail:  # a user@host is not a ref
        text, ref = head, tail
    sub: List[str] = []
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
                ref = ref or rest[1]
                sub = rest[2:]
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
            "Installing skills from a repo is off: no host is allowed "
            "(VIGIL_SKILL_SOURCE_HOSTS)"
        )
    if host not in allowed:
        raise SkillError(
            f"{host} is not an allowed skill source. Allowed: {', '.join(allowed)}"
        )
    return RepoSource(url=url, label=label, host=host, ref=ref, path="/".join(sub))


def _git_env() -> dict:
    env = dict(os.environ)  # noqa: ENV001 - child process env; keeps any proxy
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
    return env


def _git(args: List[str], cwd: Optional[Path] = None) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                # A symlink in the repo is checked out as a plain file, so nothing
                # read from the clone can point outside it.
                "-c",
                "core.symlinks=false",
                # A redirect could lead to a host the allowed list does not hold.
                "-c",
                "http.followRedirects=false",
                *args,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillError(
            f"the repo took longer than {_CLONE_TIMEOUT}s to fetch"
        ) from exc
    except OSError as exc:
        raise SkillError(f"git could not run: {exc.strerror}") from exc
    if result.returncode != 0:
        err = result.stderr
        if "could not read Username" in err or "Repository not found" in err:
            raise SkillError("No public repo by that name; private repos are not read")
        if "Remote branch" in err and "not found" in err:
            raise SkillError("The repo has no branch or tag by that name")
        lines = [line for line in err.strip().splitlines() if line.strip()]
        raise SkillError(
            f"could not fetch the repo: {lines[-1] if lines else 'git failed'}"
        )
    return result.stdout.strip()


@contextmanager
def cloned(source: RepoSource) -> Iterator[tuple[Path, str]]:
    """A shallow clone of ``source`` in a temporary folder, and its commit."""
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
            + ["--", source.url, str(repo)]
        )
        yield repo, _git(["rev-parse", "HEAD"], cwd=repo)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def discover(repo: Path, source: RepoSource) -> List[Found]:
    """The skill folders in the clone, as ``npx skills`` finds them.

    Each that cannot be installed carries why: it does not parse, another
    folder has its name, or a skill by that name is already loaded.
    """
    top = repo if not source.path else inside(repo, f"./{source.path}")
    if top is None:
        raise SkillError(f"{source.path!r} is not a folder in the repo")
    dirs = [top] if (top / SKILL_FILE).is_file() else repo_skill_dirs(top)
    if not dirs:
        raise SkillError(f"no {SKILL_FILE} found where skills are looked for")
    taken = {skill.name for skill in load_skills(skill_roots())}
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
        elif skill.name in taken:
            problem = "a skill with this name is already installed"
        else:
            try:
                folder_files(folder)  # over the size or file limit: say so up front
            except SkillError as exc:
                problem = str(exc)
        names.add(skill.name)
        found.append(Found(rel, skill.name, skill.description, problem))
    return found


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
        # Two folders can carry one name; the one discover passed is the one shown.
        found: Dict[str, Found] = {}
        for f in discover(repo, source):
            if f.name not in found or found[f.name].problem:
                found[f.name] = f
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


def _record(
    source: RepoSource,
    commit: str,
    skills: List[Skill],
    folders: dict,
    installed_by: Optional[str],
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
                SkillInstall.skill_name.in_([s.name for s in skills])
            )
        )
        session.add_all(rows)


def forget(name: str) -> None:
    """Drop where a deleted skill came from; a later skill by that name is new."""
    with unit_of_work() as session:
        session.execute(delete(SkillInstall).where(SkillInstall.skill_name == name))
