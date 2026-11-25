"""Rule libraries: detection rules imported from a git repo at a commit someone read.

A library is a ``type: "git"`` entry in ``detection_sources.json``: ``git_url``
(https), ``subdirectory``, ``format``, ``ref``, ``commit``, ``imported_by`` and
``imported_at``. That entry is the whole record. Its files are copied from a
temporary clone into Vigil's rules directory, under ``base_dir/<source id>/`` at
their paths in the repo (a git source saved before libraries existed keeps its
old clone directory), and the next sync versions them as any source's.

Repos are read with the skill reader (``core.skills.skill_sources``): its host
list, credentials, https only, and its size and time caps.

- ``read``: the commit and the rule files found, grouped by folder; for a
  library, also what changed, what is gone and what was never imported.
- ``import_rules``: picked files at the commit read, as a new library or an
  update to one, all or nothing; refused when the repo has moved since.
- ``take_whole``: a library's folders whole, at the commit read now (Update and
  ``POST /update-all``).
- ``remove``: the entry and its files.

Every write is recorded on the config audit trail before it is made.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import tempfile
import threading
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from sqlalchemy import select

from core.detections.detection_rules_service import (
    FORMAT_EXTENSIONS,
    LOCAL_ROOTS_ENV,
    DetectionRulesService,
    _host,
    _record_health,
    _repo_key,
)
from core.detections.inventory import readers
from core.skills.skill_library import SkillConflict, SkillError
from core.skills.skill_sources import RepoSource, cloned, parse_source
from core.storage.config_service import audited_change

logger = logging.getLogger(__name__)

# (user id for the audit trail, name shown as imported_by)
Who = Tuple[str, str]
SYSTEM: Who = ("system", "system")
_PICKS_MAX = 20000
_REPO_PREFIX = readers.source_name({"id": ""})  # "repo:"
_PROBLEM_MAX = 300


class LibraryError(SkillError):
    """A library read or import refused; SkillConflict is the stale-read case."""


# One library write at a time in this process: the clone runs outside it, the
# swap and the save of detection_sources.json inside.
_WRITES = threading.Lock()


def _disk_errors(fn):
    """A disk failure (full, unwritable) as a refusal naming it, not a crash."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            raise LibraryError(
                f"Could not read or write the rule files ({exc.strerror or exc}); "
                "nothing was changed"
            ) from exc

    return wrapped


# skill_sources' words for a host it could not resolve or connect to. Its text
# stays as is: skill imports have no local route to point at.
_NO_ROUTE = "could not connect to "


@contextmanager
def _clone(source: RepoSource) -> Iterator[Tuple[Path, str]]:
    """``cloned``, with a failure to reach the host naming the local route."""
    with ExitStack() as stack:
        try:
            got = stack.enter_context(cloned(source))
        except SkillError as exc:
            if _NO_ROUTE not in str(exc):
                raise
            raise LibraryError(
                f"Could not fetch {source.label}: there is no route to "
                f"{source.host} from this server. To keep these rules without "
                "one, copy them onto the server and add them as a local source "
                f"under a root listed in {LOCAL_ROOTS_ENV}."
            ) from None
        yield got


# ---- Reading ----------------------------------------------------------------


def _library(service: DetectionRulesService, source_id: str) -> Dict[str, Any]:
    source = service.get_source(source_id)
    if source is None or source.get("type") != "git":
        raise LookupError(f"No rule library {source_id}")
    return source


def _repo_of(source: Dict[str, Any]) -> RepoSource:
    """The library's repo, checked again: the allowed hosts may have changed."""
    url = source.get("git_url") or ""
    if urlsplit(url).scheme != "https":
        host = _host(url) or "its host"
        raise LibraryError(
            f"{source.get('name')}: {url or 'no URL'} is not an https URL, and repos "
            f"are read over https only. Import it again from https://{host}/… "
            f"({host} must be in VIGIL_SKILL_SOURCE_HOSTS)."
        )
    ref = source.get("ref") or ""
    repo = parse_source(f"{url}@{ref}" if ref else url)
    return replace(repo, path=(source.get("subdirectory") or "").strip("/"))


def _inside(root: Path, rel: str) -> Path:
    try:
        return Path(DetectionRulesService._under(str(root), rel or ""))
    except ValueError:
        raise LibraryError(f"{rel!r} leaves the repo") from None


def _listing(root: Path, fmt: str) -> Dict[str, Dict[str, Any]]:
    """Each rule file under ``root`` by its path: its rules, or why it does not read.

    Keys are made as sync makes them, so a clash found here is one sync would see.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not root.is_dir():
        return out
    keys: set = set()
    for path in readers.rule_files(root, fmt):
        rel = path.relative_to(root).as_posix()
        try:
            records = readers.read_file(fmt, path, rel, path.read_bytes())
        except Exception as exc:  # noqa: BLE001 -- reported on the file
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            out[rel] = _entry(rel, [], f"does not read: {first}"[:_PROBLEM_MAX])
            continue
        if not records:
            continue  # holds no rule (a README, a config file): not offered
        for record in records:
            if record.native_key in keys:
                record.native_key = f"{record.native_key}@{rel}"
            keys.add(record.native_key)
        out[rel] = _entry(rel, records)
    return out


def _entry(rel: str, records: List[Any], problem: Optional[str] = None) -> dict:
    techniques = sorted({t for r in records for t in r.techniques})
    return {
        "path": rel,
        "name": records[0].name if records else Path(rel).name,
        "language": records[0].language if records else None,
        "techniques": techniques,
        "keys": [r.native_key for r in records],
        "problem": problem,
    }


def _clashes(
    service: DetectionRulesService, found: Dict[str, dict], own: Optional[str]
) -> None:
    """Mark each file holding a key another repo source has as an active rule.

    Only repo sources: a SIEM copy shares its repo rule's id on purpose, and a
    second repo rule with the id would stop that link being made.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import DetectionRule

    keys = {k for e in found.values() if not e["problem"] for k in e["keys"]}
    if not keys:
        return
    mine = readers.source_name({"id": own}) if own else None
    held: Dict[str, str] = {}
    with get_db_manager().session_scope() as session:
        for key, source in session.execute(
            select(DetectionRule.native_key, DetectionRule.source).where(
                DetectionRule.native_key.in_(list(keys)),
                DetectionRule.status == "active",
                DetectionRule.source.like(f"{_REPO_PREFIX}%"),
            )
        ):
            if source != mine:
                held.setdefault(key, source)
    for entry in found.values():
        for key in entry["keys"]:
            if key in held and not entry["problem"]:
                other = service.get_source(held[key].removeprefix(_REPO_PREFIX))
                entry["problem"] = (
                    f"{key} is already an active rule of "
                    f"{other['name'] if other else held[key]}"
                )


def _adopted(source_id: str, found: Dict[str, dict]) -> None:
    """Note, on each rule a proposal adopted at an earlier version, that version."""
    from core.storage.connection import get_db_manager
    from core.storage.models import DetectionRule
    from core.storage.models.detection_proposal import DetectionProposal, ProposalState

    by_key = {k: e for e in found.values() for k in e["keys"]}
    if not by_key:
        return
    with get_db_manager().session_scope() as session:
        rules = {
            r.id: r
            for r in session.execute(
                select(
                    DetectionRule.id,
                    DetectionRule.native_key,
                    DetectionRule.latest_version,
                ).where(
                    DetectionRule.source == readers.source_name({"id": source_id}),
                    DetectionRule.native_key.in_(list(by_key)),
                )
            )
        }
        if not rules:
            return
        rows = session.execute(
            select(DetectionProposal.rule_id, DetectionProposal.origin).where(
                DetectionProposal.rule_id.in_(list(rules)),
                DetectionProposal.state != ProposalState.REJECTED,
            )
        ).all()
    for rule_id, origin in rows:
        rule = rules[rule_id]
        version = (origin or {}).get("rule_version")
        if isinstance(version, int) and version < rule.latest_version:
            by_key[rule.native_key]["adopted_version"] = version


def _disk_files(here: Path, fmt: str) -> Dict[str, Path]:
    """The rule files under ``here`` by path, none when it is missing; .git is ignored."""
    if not here.is_dir():
        return {}
    return {p.relative_to(here).as_posix(): p for p in readers.rule_files(here, fmt)}


def _diff(
    there: Path, here: Path, fmt: str, found: Dict[str, dict]
) -> Dict[str, List[dict]]:
    """The clone's rule files against the library's, file by file."""
    theirs = _disk_files(there, fmt)
    mine = _disk_files(here, fmt)

    def offered(rel: str) -> dict:
        e = found.get(rel)
        return {
            "path": rel,
            "name": e["name"] if e else Path(rel).name,
            "problem": e["problem"] if e else "holds no rule",
        }

    return {
        "changed": [
            offered(rel)
            for rel in sorted(theirs)
            if rel in mine and theirs[rel].read_bytes() != mine[rel].read_bytes()
        ],
        "gone": [
            {"path": rel, "name": Path(rel).name}
            for rel in sorted(mine)
            if rel not in theirs
        ],
        "new": [offered(rel) for rel in sorted(theirs) if rel not in mine],
    }


def _grouped(found: Dict[str, dict]) -> List[dict]:
    folders: Dict[str, List[dict]] = {}
    for rel in sorted(found):
        folders.setdefault(rel.rpartition("/")[0], []).append(found[rel])
    return [{"folder": f, "rules": rules} for f, rules in sorted(folders.items())]


def _format(fmt: Optional[str]) -> str:
    fmt = fmt or "auto"
    if fmt not in FORMAT_EXTENSIONS:
        raise LibraryError(
            f"{fmt!r} is not a rule format: use one of {', '.join(FORMAT_EXTENSIONS)}"
        )
    return fmt


@_disk_errors
def read(
    service: DetectionRulesService,
    repo: Optional[str] = None,
    source_id: Optional[str] = None,
    fmt: Optional[str] = None,
) -> Dict[str, Any]:
    """What importing from ``repo`` (owner/repo, a GitHub link, an https URL,
    ``@ref``), or updating the library ``source_id``, would offer."""
    if source_id:
        service.refresh()
    library = _library(service, source_id) if source_id else None
    if library is not None:
        source, fmt = _repo_of(library), library.get("format") or "auto"
    elif repo:
        source, fmt = parse_source(repo), _format(fmt)
    else:
        raise LibraryError("Name a repo to read, or a library to update")
    with _clone(source) as (clone, commit):
        root = _inside(clone, source.path)
        if not root.is_dir() and library is None:
            raise LibraryError(f"{source.path!r} is not a folder in {source.label}")
        # A library whose folder left the repo reads as every file gone.
        found = _listing(root, fmt)
        diff = None
        if library is not None:
            here = _inside(Path(library["local_path"]), source.path)
            diff = _diff(root, here, fmt, found)
    _clashes(service, found, library["id"] if library else None)
    if library is not None:
        _adopted(library["id"], found)
        # A clash or a read problem found after the diff was built.
        for group in (diff["changed"], diff["new"]):
            for change in group:
                if change["path"] in found:
                    change["problem"] = found[change["path"]]["problem"]
    return {
        "repo": source.label,
        "url": source.url,
        "ref": source.ref or "",
        "path": source.path,
        "commit": commit,
        "format": fmt,
        "rule_files": len(found),
        "folders": _grouped(found),
        "library": (
            None
            if library is None
            else {
                "id": library["id"],
                "name": library.get("name"),
                "commit": library.get("commit"),
                **diff,
            }
        ),
    }


# ---- Writing ----------------------------------------------------------------


def _owned(service: DetectionRulesService, source: Dict[str, Any]) -> Path:
    """The library's directory, which must sit under Vigil's rules directory."""
    try:
        path = Path(service._under(str(service.base_dir), source["local_path"]))
        if source["local_path"] and path != Path(os.path.realpath(service.base_dir)):
            return path
    except (KeyError, ValueError):
        pass
    raise LibraryError(
        f"{source.get('name')}'s files are not in Vigil's rules directory "
        f"({service.base_dir}); Vigil does not write them"
    ) from None


def _scratch(service: DetectionRulesService, kind: str) -> Path:
    """A folder beside the libraries, so each swap is a rename on one filesystem."""
    service.base_dir.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{kind}-", dir=service.base_dir))


@contextmanager
def _swapping() -> Iterator[List[Tuple[Path, Optional[Path]]]]:
    """Undo every swap recorded in the list when the block raises.

    Each record is (target, backup): the target is moved aside to backup (None
    when there was nothing) before the new copy goes in.
    """
    done: List[Tuple[Path, Optional[Path]]] = []
    try:
        yield done
    except BaseException as exc:
        for target, backup in reversed(done):
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
                if backup is not None:
                    os.replace(backup, target)
            except OSError:
                logger.exception("could not put %s back", target)
        if isinstance(exc, OSError):
            raise LibraryError(
                f"Could not write the library ({exc.strerror}); " "nothing was changed"
            ) from exc
        raise


def _swap(
    done: List[Tuple[Path, Optional[Path]]],
    target: Path,
    staged: Optional[Path],
    backups: Path,
) -> None:
    """Move ``target`` aside and ``staged`` (None: nothing) into its place."""
    backup: Optional[Path] = None
    if target.exists() or target.is_symlink():
        backup = backups / str(len(done))
        os.replace(target, backup)
    done.append((target, backup))
    if staged is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)


def _short(commit: Optional[str]) -> Optional[str]:
    # The trail blanks any 32+ hex run as a possible secret; 12 places stay readable.
    return commit[:12] if commit else commit


def _record(source: Dict[str, Any]) -> dict:
    """What the trail keeps of a library."""
    keys = ("id", "name", "type", "format", "git_url", "local_path", "ref")
    return {
        **{k: source.get(k) for k in keys},
        "commit": _short(source.get("commit")),
        "subdirectory": source.get("subdirectory") or "",
        "deploy_target": bool(source.get("deploy_target")),
    }


def _settle(
    service: DetectionRulesService, source: Dict[str, Any], commit: str, who: Who
) -> Dict[str, Any]:
    """Record the commit taken and rescan the library's rules; raises when the
    entry cannot be saved, so the caller puts the files back.

    The entry is looked up again in the sources as saved now: a read while the
    clone ran may have replaced this instance's list, and another process may
    have saved since. A new library is added here.
    """
    service.refresh()
    current = service.get_source(source["id"])
    if current is None:
        service.sources.append(source)
        current = source
    elif current is not source:
        current.update({k: v for k, v in source.items() if k not in current})
    source = current
    now = datetime.now().isoformat()
    source.update(
        commit=commit,
        imported_by=who[1],
        imported_at=now,
        last_updated=now,
        status="ready",
        rule_count=service._count_rules(
            Path(source["local_path"]),
            source.get("format") or "auto",
            source.get("subdirectory") or "",
        ),
    )
    try:
        service._save_config(strict=True)
    except Exception:
        service.refresh()  # drop the unsaved change; the caller puts files back
        raise
    try:
        _record_health(source)
    except Exception:  # noqa: BLE001 -- taken; only Integration Health lags
        logger.exception("could not record health for %s", source.get("name"))
    return source


def _check_picks(picks: List[str]) -> None:
    if len(set(picks)) != len(picks) or len(picks) > _PICKS_MAX:
        raise LibraryError("Each rule file can be picked once")


def _moved(source: RepoSource, got: str, commit: str) -> None:
    if got != commit:
        raise SkillConflict(
            f"{source.label} has changed since it was read (now {got[:7]}). "
            "Read it again before importing."
        )


def _not_imported(service: DetectionRulesService, source: RepoSource) -> None:
    """Refuse a repo, ref and folder that is already a library."""
    for other in service.list_sources():
        if (
            other.get("type") == "git"
            and _repo_key(other.get("git_url") or "") == _repo_key(source.url)
            and (other.get("ref") or "") == (source.ref or "")
            and (other.get("subdirectory") or "").strip("/") == source.path
        ):
            raise SkillConflict(
                f"{source.label} is already the library {other.get('name')!r}; "
                "read that library to update it"
            )


@_disk_errors
def import_rules(
    service: DetectionRulesService,
    commit: str,
    picks: List[str],
    who: Who,
    repo: Optional[str] = None,
    source_id: Optional[str] = None,
    name: Optional[str] = None,
    fmt: Optional[str] = None,
) -> Dict[str, Any]:
    """Import the rule files ``picks`` at ``commit``, the commit that was read.

    With ``repo``, a new library: zero picks make an empty one (a deploy target
    can start that way). With ``source_id``, an update to that library: each
    pick is a file it changed, added (In the repo, not imported) or dropped
    (Gone, which deletes it). Paths are relative to the library's folder in
    the repo. All or nothing: a failure leaves the files and the entry as they
    were. Returns the library.
    """
    _check_picks(picks)
    service.refresh()  # another process may have saved a change since
    if source_id:
        return _update(service, _library(service, source_id), commit, picks, who)
    if not repo:
        raise LibraryError("Name a repo to import from, or a library to update")
    fmt = _format(fmt)
    source = parse_source(repo)
    _not_imported(service, source)
    source_id = str(uuid.uuid4())[:8]
    dest = Path(service._under(str(service.base_dir), source_id))
    stage = _scratch(service, "import")
    try:
        tree = stage / "library"
        rules = _inside(tree, source.path)
        rules.mkdir(parents=True)
        with _clone(source) as (clone, got):
            _moved(source, got, commit)
            root = _inside(clone, source.path)
            if not root.is_dir():
                raise LibraryError(f"{source.path!r} is not a folder in {source.label}")
            found = _listing(root, fmt)
            _clashes(service, found, None)
            for pick in picks:
                entry = found.get(pick)
                if entry is None:
                    raise LibraryError(f"{pick!r} is not a rule file in {source.label}")
                if entry["problem"]:
                    raise SkillConflict(f"{pick!r}: {entry['problem']}")
                _copy(root / pick, _inside(rules, pick))
        library = {
            "id": source_id,
            "name": (name or "").strip() or source.label,
            "type": "git",
            "git_url": source.url,
            "format": fmt,
            "subdirectory": source.path,
            "story_subdirectory": "",
            "clone_name": source_id,
            "local_path": str(dest),
            "rule_count": 0,
            "last_updated": None,
            "status": "ready",
            "deploy_target": False,
            "github_enterprise": False,
            "ref": source.ref or "",
        }
        with _WRITES, audited_change(
            who[0],
            config_type="detection_rules",
            config_key=f"source:{source_id}",
            action="create",
            old_value=None,
            new_value={
                **_record(library),
                "commit": _short(commit),
                "picks": len(picks),
            },
        ):
            service.refresh()
            _not_imported(service, source)  # another import may have won the race
            os.replace(tree, dest)
            try:
                library = _settle(service, library, commit, who)
            except BaseException:
                shutil.rmtree(dest, ignore_errors=True)
                raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    logger.info(
        "Imported %d rule files from %s as %s", len(picks), source.label, source_id
    )
    return library


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _update(
    service: DetectionRulesService,
    library: Dict[str, Any],
    commit: str,
    picks: List[str],
    who: Who,
) -> Dict[str, Any]:
    if not picks:
        raise LibraryError("Pick at least one change to take")
    source = _repo_of(library)
    fmt = library.get("format") or "auto"
    here = _inside(_owned(service, library), source.path)
    stage = _scratch(service, "update")
    try:
        staged, backups = stage / "files", stage / "backup"
        backups.mkdir()
        with _clone(source) as (clone, got):
            _moved(source, got, commit)
            root = _inside(clone, source.path)
            found = _listing(root, fmt)
            _clashes(service, found, library["id"])
            diff = _diff(root, here, fmt, found)
            offered = {c["path"]: (kind, c) for kind in diff for c in diff[kind]}
            for pick in picks:
                if pick not in offered:
                    raise SkillConflict(f"{pick!r} has nothing to take; read it again")
                kind, change = offered[pick]
                if kind != "gone" and change.get("problem"):
                    raise SkillConflict(f"{pick!r}: {change['problem']}")
                if kind != "gone":
                    _copy(root / pick, _inside(staged, pick))
        with _WRITES, audited_change(
            who[0],
            config_type="detection_rules",
            config_key=f"source:{library['id']}",
            action="update",
            old_value=_record(library),
            new_value={
                **_record(library),
                "commit": _short(commit),
                **{
                    kind: sorted(p for p in picks if offered[p][0] == kind)
                    for kind in ("changed", "new", "gone")
                },
            },
        ):
            with _swapping() as done:
                for pick in picks:
                    gone = offered[pick][0] == "gone"
                    _swap(
                        done,
                        _inside(here, pick),
                        None if gone else _inside(staged, pick),
                        backups,
                    )
                library = _settle(service, library, commit, who)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    logger.info("Took %d changes to %s at %s", len(picks), library["name"], commit[:7])
    return library


@_disk_errors
def take_whole(
    service: DetectionRulesService, library: Dict[str, Any], who: Optional[Who] = None
) -> Dict[str, Any]:
    """Take the library's folders whole at the commit read now: its rule folder
    and, when it has one, its story folder (Splunk stories feed STORY_PATHS).

    Update on a library and ``POST /update-all`` come here. Each folder is
    copied out of the clone, then swapped in; a failure puts back what was there.
    """
    who = who or SYSTEM
    source = _repo_of(library)
    local = _owned(service, library)
    sub = source.path
    folders = [sub]
    story = (library.get("story_subdirectory") or "").strip("/")
    # A story folder inside the rule folder already comes with it.
    if story and sub and not (story + "/").startswith(sub + "/"):
        folders.append(story)
    stage = _scratch(service, "update")
    try:
        backups = stage / "backup"
        backups.mkdir()
        staged: List[Tuple[Path, Path]] = []
        with _clone(source) as (clone, commit):
            for i, folder in enumerate(folders):
                there = _inside(clone, folder)
                if not there.is_dir():
                    if folder == sub:
                        raise LibraryError(
                            f"{folder!r} is not a folder in {source.label}"
                        )
                    continue  # a story folder the repo dropped: leave the one here
                copy = stage / f"copy{i}"
                shutil.copytree(there, copy, ignore=shutil.ignore_patterns(".git"))
                staged.append((_inside(local, folder), copy))
        with _WRITES, audited_change(
            who[0],
            config_type="detection_rules",
            config_key=f"source:{library['id']}",
            action="update",
            old_value=_record(library),
            new_value={**_record(library), "commit": _short(commit), "whole": True},
        ):
            with _swapping() as done:
                local.mkdir(parents=True, exist_ok=True)
                for target, copy in staged:
                    _swap(done, target, copy, backups)
                library = _settle(service, library, commit, who)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    logger.info("Took %s whole at %s", library["name"], commit[:7])
    return library


@_disk_errors
def remove(service: DetectionRulesService, source_id: str, who: Who) -> Dict[str, Any]:
    """Remove the library's entry and its files. The next sync marks its rules
    removed; proposals and overlay copies made from them stay."""
    service.refresh()
    library = _library(service, source_id)
    with _WRITES, audited_change(
        who[0],
        config_type="detection_rules",
        config_key=f"source:{source_id}",
        action="delete",
        old_value=_record(library),
        new_value={"delete_files": True},
    ):
        if not service.remove_source(source_id, delete_files=True):
            raise LookupError(f"No rule library {source_id}")
    return {"removed": source_id, "name": library.get("name")}


def sync_soon() -> None:
    """Sync the rule tables in the background, as the routes do after a change."""

    def run() -> None:
        from core.detections.inventory.sync import sync_repos

        try:
            sync_repos()
        except Exception as exc:  # noqa: BLE001 -- the hourly daemon sync retries
            logger.warning("Detection rule sync failed: %s", exc)

    threading.Thread(target=run, name="rule-library-sync", daemon=True).start()
