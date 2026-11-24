"""Skills as directories of ``SKILL.md`` (epic #882, #925).

A skill is ``<root>/<name>/SKILL.md`` conforming to the public Agent Skills
spec (agentskills.io/specification): YAML frontmatter naming the skill and a
Markdown body telling the agent how to do the thing. Roots are the bundled
``core/skills/library/`` plus the operator root: ``VIGIL_SKILLS_PATH``, else
the State Directory's ``skills/`` folder. A top-level folder without its own
``SKILL.md`` is searched where the ``npx skills`` CLI searches a repo, so a
library grouped in category folders, or a whole cloned skills repo, loads
as-is. A skill folder's subfolders are never skills.

No database, no module state: ``load_skills`` takes its roots so tests can
point it at ``tmp_path``. An invalid skill is logged and skipped so one bad
directory cannot take the library down with it.
"""

from __future__ import annotations

import errno
import io
import json
import logging
import lzma
import os
import posixpath
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Collection, Dict, Iterable, List, Optional

import yaml

from core.config import Settings, get_settings, vigil_path
from core.frontmatter import FrontmatterError, split_frontmatter
from core.intent import declared_dir, read_revision

logger = logging.getLogger(__name__)

READ_SKILL_TOOL = "read_skill"
SKILL_FILE = "SKILL.md"
LIBRARY_ROOT = Path(__file__).resolve().parent / "library"

# The spec's name grammar: lowercase letters, digits and single hyphens, never
# at either end. The length bound is checked separately for a clearer message.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_NAME_MAX = 64
_DESCRIPTION_MAX = 1024
_COMPATIBILITY_MAX = 500
_FILES_MAX = 200
UPLOAD_MAX = 5 * 1024 * 1024  # bytes read from an upload
_UNPACKED_MAX = 5 * 1024 * 1024  # bytes across a zip's files once unpacked
# Skill folders the skills API may hold under the operator root: a save or
# upload past it is refused. Each description is in every agent's system
# prompt. Folders copied in by hand are not capped.
OPERATOR_SKILLS_MAX = 100
# The constants below are where a skill may sit inside a top-level folder of a
# skills root: the places the ``npx skills`` CLI (1.8.1) searches in a repo, so
# a folder copied or cloned in loads what ``npx skills add`` would install.
_SKIP_DIRS = frozenset({"node_modules", ".git", "dist", "build", "__pycache__"})
_SKILLS_DIRS = ("skills", "skills/.curated", "skills/.experimental", "skills/.system")
_AGENT_SKILL_DIRS = (
    ".adal/skills",
    ".agents/skills",
    ".aider-desk/skills",
    ".augment/skills",
    ".autohand/skills",
    ".bob/skills",
    ".claude/skills",
    ".cline/skills",
    ".codeartsdoer/skills",
    ".codebuddy/skills",
    ".codemaker/skills",
    ".codestudio/skills",
    ".codex/skills",
    ".commandcode/skills",
    ".continue/skills",
    ".cortex/skills",
    ".crush/skills",
    ".devin/skills",
    ".factory/skills",
    ".forge/skills",
    ".github/skills",
    ".goose/skills",
    ".grok/skills",
    ".hermes/skills",
    ".iflow/skills",
    ".inferencesh/skills",
    ".jazz/skills",
    ".junie/skills",
    ".kilo/skills",
    ".kilocode/skills",
    ".kimchi/skills",
    ".kiro/skills",
    ".kode/skills",
    ".lingma/skills",
    ".mcpjam/skills",
    ".minimax/skills",
    ".moxby/skills",
    ".mux/skills",
    ".neovate/skills",
    ".ona/skills",
    ".opencode/skills",
    ".openhands/skills",
    ".pi/skills",
    ".pochi/skills",
    ".posit/assistant/skills",
    ".qoder/skills",
    ".qwen/skills",
    ".reasonix/skills",
    ".roo/skills",
    ".rovodev/skills",
    ".tabnine/agent/skills",
    ".terramind/skills",
    ".tinycloud/skills",
    ".trae/skills",
    ".vibe/skills",
    ".windsurf/skills",
    ".zcode/skills",
    ".zencoder/skills",
    "agent/skills",
    "data/skills",
)
_CONTAINER_DEPTH = 3  # skills/<category>/<category>/<name>/SKILL.md
_FALLBACK_DEPTH = 5  # how deep a folder with none of those places is searched
# Folders the fallback looks in per top-level folder, and the bytes of a manifest
# read. Not npx limits: this search runs on every load, not once at install.
_FALLBACK_DIRS_MAX = 1000
_MANIFEST_MAX = 64 * 1024


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the skill directory; SKILL.md and references/ live under it


class SkillError(ValueError):
    """The directory is not a spec-conformant skill, or a write was refused."""


class SkillNotFound(SkillError):
    """No loaded skill has this name."""


class SkillConflict(SkillError):
    """The write clashes with what is on disk: a stale version, a taken folder or a full root."""


def _require_str(frontmatter: Dict[str, Any], key: str, limit: int) -> str:
    value = frontmatter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillError(f"`{key}` must be a non-empty string")
    if len(value) > limit:
        raise SkillError(f"`{key}` is longer than {limit} characters")
    return value


# A key written with no value parses to None and is read as absent.
def _check_optional(frontmatter: Dict[str, Any]) -> None:
    for key, limit in (("license", None), ("compatibility", _COMPATIBILITY_MAX)):
        value = frontmatter.get(key)
        if value is not None and not isinstance(value, str):
            raise SkillError(f"`{key}` must be a string")
        if limit and len(value or "") > limit:
            raise SkillError(f"`{key}` is longer than {limit} characters")
    tools = frontmatter.get("allowed-tools")
    if tools is not None and not isinstance(tools, str):
        raise SkillError("`allowed-tools` must be a space-delimited string")
    metadata = frontmatter.get("metadata")
    if metadata is not None and not (
        isinstance(metadata, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())
    ):
        raise SkillError("`metadata` must be a map of string to string")


def parse_skill(skill_dir: Path, *, match_dir: bool = True) -> Skill:
    """Validate ``skill_dir/SKILL.md`` against the spec and return the skill.

    Raises :class:`SkillError` for anything the spec forbids. Keys the spec does
    not name are left alone: rejecting them would make the loader stricter than
    the format it claims to read. ``match_dir`` off skips the folder-name rule,
    for a SKILL.md at a repo's root, which is installed under its own name.
    """
    skill_file = skill_dir / SKILL_FILE
    if not skill_file.is_file():
        raise SkillError(f"no {SKILL_FILE}")
    try:
        frontmatter, _ = split_frontmatter(skill_file.read_text(encoding="utf-8-sig"))
    except (FrontmatterError, UnicodeDecodeError, OSError) as exc:
        raise SkillError(str(exc)) from exc
    if frontmatter is None:
        raise SkillError("missing YAML frontmatter")

    name = _require_str(frontmatter, "name", _NAME_MAX)
    if not _NAME_RE.match(name):
        raise SkillError(
            f"`name` {name!r} must be lowercase letters, digits and single "
            "hyphens, not starting or ending with one"
        )
    if match_dir and name != skill_dir.name:
        raise SkillError(f"`name` {name!r} does not match directory {skill_dir.name!r}")
    description = _require_str(frontmatter, "description", _DESCRIPTION_MAX)
    _check_optional(frontmatter)
    return Skill(name=name, description=description, path=skill_dir)


def as_user_turn(user_input: Any) -> str:
    """The user turn an eval sends: a string as itself, otherwise indented JSON."""
    return (
        user_input if isinstance(user_input, str) else json.dumps(user_input, indent=2)
    )


def _subdirs(folder: Path) -> List[Path]:
    """``folder``'s real subfolders, sorted. Symlinks are not followed: one could
    reach outside the root that writes and deletes are confined to."""
    try:
        with os.scandir(folder) as it:
            return sorted(Path(e.path) for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def _inside(base: Path, rel: Any) -> Optional[Path]:
    """``base/rel`` for a ``./`` path that stays inside ``base`` once
    normalized, when that is a real folder reached without a symlink; ``./`` is
    ``base`` itself. A manifest written by anyone can name it, so any odd path
    is None rather than an error."""
    if not isinstance(rel, str) or not rel.startswith("./") or "\\" in rel:
        return None
    norm = posixpath.normpath(rel)
    if norm == ".." or norm.startswith("../"):
        return None
    path = base
    try:
        for part in [] if norm == "." else norm.split("/"):
            path = path / part
            if path.is_symlink():
                return None
        return path if path.is_dir() else None
    except (OSError, ValueError):  # a name too long, or holding a NUL
        return None


def _json(path: Path) -> Any:
    """A manifest's JSON, or None. One over ``_MANIFEST_MAX`` is ignored: a
    cloned repo's manifest is read on every load."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MANIFEST_MAX + 1)
        if len(raw) > _MANIFEST_MAX:
            return None
        return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # nested past the parser's limit
        return None


def _listed_dirs(top: Path) -> tuple[List[Path], List[Path]]:
    """Folders ``top``'s own manifests point at, as ``npx skills`` reads them.

    ``skills.sh.json`` ``paths`` are searched like ``skills/``. A
    ``.claude-plugin`` ``plugin.json`` or ``marketplace.json`` plugin's
    ``skills`` entries name skill folders, whose parent folder is searched
    one level, as is the plugin's ``skills/`` folder. Only ``./`` paths that
    stay inside ``top`` count.
    """
    containers: List[Path] = []
    plugin_dirs: List[Path] = []
    config = _json(top / "skills.sh.json")
    if isinstance(config, dict) and isinstance(config.get("paths"), list):
        for rel in config["paths"]:
            folder = _inside(top, rel)
            # npx skips a path that is the repo, and a path listed twice.
            if folder and folder != top and folder not in containers:
                containers.append(folder)

    def add_plugin(base: Optional[Path], skills: Any) -> None:
        if base is None:
            return
        for rel in skills if isinstance(skills, list) else []:
            if isinstance(rel, str) and rel.startswith("./"):
                parent = posixpath.dirname(posixpath.normpath(rel))
                folder = _inside(base, f"./{parent}")
                if folder and folder not in plugin_dirs:
                    plugin_dirs.append(folder)
        folder = _inside(base, "./skills")
        if folder and folder not in plugin_dirs:
            plugin_dirs.append(folder)

    market = _json(top / ".claude-plugin" / "marketplace.json")
    if isinstance(market, dict):
        meta = market.get("metadata")
        plugin_root = meta.get("pluginRoot", "./") if isinstance(meta, dict) else "./"
        root = _inside(top, plugin_root)
        for item in market.get("plugins") or []:
            if isinstance(item, dict) and root is not None:
                add_plugin(_inside(root, item.get("source", "./")), item.get("skills"))
    manifest = _json(top / ".claude-plugin" / "plugin.json")
    if isinstance(manifest, dict):
        add_plugin(top, manifest.get("skills"))
    return containers, plugin_dirs


def _parses(folder: Path) -> bool:
    try:
        parse_skill(folder)
    except SkillError:
        return False
    return True


def _source_skill_dirs(top: Path) -> List[Path]:
    """The skill folders ``npx skills`` would find in ``top`` taken as a repo.

    Its children; ``skills/`` (with ``.curated``, ``.experimental`` and
    ``.system``), each agent's ``.<agent>/skills`` and any ``skills.sh.json``
    path down to two category folders; folders a plugin manifest names. Only
    when none of those holds a valid skill is ``top`` searched down to
    ``_FALLBACK_DEPTH`` folders, looking in at most ``_FALLBACK_DIRS_MAX``.
    ``_SKIP_DIRS`` are never searched, a skill folder's own subfolders are its
    files, never skills, and a symlinked ``SKILL.md`` is not a skill.
    """
    found: List[Path] = []
    seen: set[Path] = set()

    def add(folder: Path) -> bool:
        skill_md = os.path.join(folder, SKILL_FILE)  # os.path: called per folder
        # A symlinked SKILL.md could read a file outside the root into a prompt.
        if os.path.islink(skill_md) or not os.path.isfile(skill_md):
            return False
        if folder not in seen:
            seen.add(folder)
            found.append(folder)
        return True

    checked = [0]  # folders the fallback has looked in

    def walk(
        folder: Path, max_depth: int, depth: int = 1, fallback: bool = False
    ) -> None:
        for child in _subdirs(folder):
            if fallback:
                checked[0] += 1
                if checked[0] > _FALLBACK_DIRS_MAX or child.name in _SKIP_DIRS:
                    continue
            if add(child) or depth >= max_depth or child.name in _SKIP_DIRS:
                continue
            walk(child, max_depth, depth + 1, fallback)

    walk(top, 1)
    present = {child.name for child in _subdirs(top)}
    for rel in _SKILLS_DIRS + _AGENT_SKILL_DIRS:
        # Probe only what ``top`` holds: this runs for every folder on every load.
        folder = _inside(top, "./" + rel) if rel.split("/")[0] in present else None
        if folder:
            walk(folder, _CONTAINER_DEPTH)
    containers, plugin_dirs = _listed_dirs(top)
    for folder in containers:
        walk(folder, _CONTAINER_DEPTH)
    for folder in plugin_dirs:
        walk(folder, 1)
    if not any(_parses(folder) for folder in found):
        walk(top, _FALLBACK_DEPTH, fallback=True)
    return found


def _skill_dirs(root: Path) -> tuple[List[Path], List[tuple[Path, str]]]:
    """The folders under ``root`` holding ``SKILL.md``, in load order, and those skipped.

    A top-level folder holding ``SKILL.md`` is a skill; any other is searched
    as ``npx skills`` searches a repo (``_source_skill_dirs``), so a library
    grouped in category folders, or a whole cloned skills repo, loads as-is.
    Dot folders at the top are a write's scratch copies and are left out. A
    raise means ``root`` itself could not be read. Nothing is logged here, so
    the writes that walk the root stay quiet; ``load_skills`` logs once.
    """
    found: List[Path] = []
    skipped: List[tuple[Path, str]] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if child.name.startswith("."):
            continue
        if os.path.lexists(child / SKILL_FILE):
            found.append(child)
        elif child.is_symlink():
            skipped.append((child, "a symlinked folder"))
        else:
            inner = _source_skill_dirs(child)
            if inner:
                found += inner
            else:
                skipped.append((child, f"no {SKILL_FILE} where npx skills would look"))
    return found, skipped


def load_skills(roots: Iterable[Path]) -> List[Skill]:
    """Every valid skill under ``roots``, in root then folder order.

    Roots earlier in the list win a name clash, so the bundled library cannot be
    shadowed by the setting root; within a root the first folder wins. A missing
    root is not an error: the bundled directory may be empty and the setting
    root is optional.
    """
    loaded: Dict[str, Skill] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            logger.debug("Skills root %s is not a directory; skipping", root)
            continue
        skill_dirs, skipped = _skill_dirs(root)
        for path, reason in skipped:
            logger.warning("Skipping folder at %s: %s", path, reason)
        for skill_dir in skill_dirs:
            try:
                skill = parse_skill(skill_dir)
            except SkillError as exc:
                logger.warning("Skipping skill at %s: %s", skill_dir, exc)
                continue
            if skill.name in loaded:
                logger.warning(
                    "Skipping skill at %s: name %r already loaded from %s",
                    skill_dir,
                    skill.name,
                    loaded[skill.name].path,
                )
                continue
            loaded[skill.name] = skill
    return list(loaded.values())


def skill_roots(settings: Optional[Settings] = None) -> List[Path]:
    """The bundled library, then the operator root unless operator skills are off."""
    settings = settings or get_settings()
    roots = [LIBRARY_ROOT]
    operator = operator_skills_root(settings)
    if operator is not None:
        roots.append(operator)
    return roots


def operator_skills_root(settings: Optional[Settings] = None) -> Optional[Path]:
    """The operator skills directory, or None when operator skills are off.

    ``VIGIL_SKILLS_PATH`` when set. Unset, the State Directory's ``skills/``
    folder (``~/.vigil/skills``); set empty, operator skills are off. Nothing is
    created here: the first write makes the default folder.
    """
    settings = settings or get_settings()
    if settings.vigil_skills_path is None:
        # The directory itself, not vigil_path("skills"): that would read a
        # legacy ~/.deeptempo copy while writes went to ~/.vigil.
        return vigil_path() / "skills"
    raw = settings.vigil_skills_path.strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def is_bundled(skill: Skill) -> bool:
    """True when the skill directory lives under the bundled library."""
    try:
        return skill.path.resolve().is_relative_to(LIBRARY_ROOT.resolve())
    except OSError:
        return False


def skill_body(skill: Skill) -> str:
    """The Markdown under the frontmatter, which is what the drawer edits."""
    content = (skill.path / SKILL_FILE).read_text(encoding="utf-8-sig")
    _, offset = split_frontmatter(content)
    return content[offset:].lstrip("\n")


def _frontmatter_of(skill_dir: Path) -> Dict[str, Any]:
    """The frontmatter of ``skill_dir/SKILL.md``, or ``{}`` when it has none."""
    try:
        text = (skill_dir / SKILL_FILE).read_text(encoding="utf-8-sig")
        return split_frontmatter(text)[0] or {}
    except (FrontmatterError, UnicodeDecodeError, OSError):
        return {}


def _version_of(frontmatter: Dict[str, Any]) -> int:
    """``metadata.version`` as a positive int; missing or malformed counts as 1."""
    metadata = frontmatter.get("metadata")
    raw = metadata.get("version") if isinstance(metadata, dict) else None
    ok = (
        isinstance(raw, str) and raw.isdecimal() and len(raw) <= 9
    )  # bounded: int() of a huge digit string raises
    return int(raw) if ok and int(raw) > 0 else 1


def skill_version(skill: Skill) -> int:
    return _version_of(_frontmatter_of(skill.path))


def skill_files(skill: Skill) -> List[Dict[str, Any]]:
    """Regular files in the skill folder as ``{path, size}``: SKILL.md, then sorted.

    Symlinks and dotfiles are skipped, and the count is capped.
    """
    found: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(skill.path):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".") and not Path(dirpath, d).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(dirpath, filename)
            if filename.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(skill.path).as_posix()
            found.append({"path": rel, "size": path.stat().st_size})
    found.sort(key=lambda f: (f["path"] != SKILL_FILE, f["path"]))
    return found[:_FILES_MAX]


def render_skill_markdown(
    name: str,
    description: str,
    body: str,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """``SKILL.md`` with ``name`` and ``description`` first, then ``extra`` as given."""
    keys = {k: v for k, v in (extra or {}).items() if k not in ("name", "description")}
    dumped = yaml.safe_dump(
        {"name": name, "description": description, **keys},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    text = f"---\n{dumped}---\n"
    if body:
        if not body.startswith("\n"):
            text += "\n"
        text += body
        if not text.endswith("\n"):
            text += "\n"
    return text


def _library_names() -> set[str]:
    return {skill.name for skill in load_skills([LIBRARY_ROOT])}


def _contained(root: Path, path: Path) -> bool:
    return path != root and path.is_relative_to(root)


def _direct_child(root: Path, name: str) -> Path:
    """``root/name`` as a real path, refused when it would leave ``root``.

    The name is normalized and required to stay under ``root`` before any
    filesystem access, so a separator or ``..`` cannot choose another path.
    A symlink is refused after that check and before ``realpath`` follows it.
    """
    if (
        not name
        or name != Path(name).name
        or name in {".", ".."}
        or not _NAME_RE.match(name)
        or len(name) > _NAME_MAX
    ):
        raise SkillError(f"invalid skill name {name!r}")
    base = os.path.realpath(os.fspath(root))
    joined = os.path.normpath(os.path.join(base, name))
    prefix = base + os.sep if not base.endswith(os.sep) else base
    if not joined.startswith(prefix):
        raise SkillError("resolved path leaves the operator skills root")
    link = Path(joined)
    if link.is_symlink():
        raise SkillError("refusing to follow a symlink")
    candidate = os.path.realpath(joined)
    if not candidate.startswith(prefix):
        raise SkillError("resolved path leaves the operator skills root")
    path = Path(candidate)
    if path.parent != Path(base) or path.name != name:
        raise SkillError("resolved path leaves the operator skills root")
    return path


def _skill_dir(root: Path, name: str) -> Path:
    """The operator directory for ``name``, never the bundled library."""
    candidate = _direct_child(root, name)
    library = Path(os.path.realpath(LIBRARY_ROOT))
    if candidate == library or candidate.is_relative_to(library):
        raise SkillError("refusing to write into the bundled library")
    return candidate


def _loaded_dir(root: Path, skill: Skill) -> Path:
    """A loaded operator skill's real folder: ``root/[<folder>/...]name``.

    Refused when a folder on the way is a symlink, the top one is a dot folder,
    or it would resolve outside ``root``, so an edit or delete acts only inside
    the operator root.
    """
    try:
        parts = skill.path.relative_to(root).parts
    except ValueError:
        parts = ()
    if not parts or parts[-1] != skill.name or parts[0].startswith("."):
        raise SkillError("refusing to touch a skill outside the operator root")
    parent = Path(os.path.realpath(root))
    for part in parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or not parent.is_dir():
            raise SkillError("refusing to follow a symlink")
    return _skill_dir(parent, skill.name)


def _operator_skill_dir(root: Path, name: str) -> Path:
    """Where operator skill ``name`` lives, else ``root/name`` for a new one.

    An existing skill is found where it loaded from, so editing one inside a
    category writes to that folder rather than starting a second copy at the
    top. A category folder of the same name is refused: a ``SKILL.md`` written
    into it would hide every skill under it. Only folders named ``name`` are
    parsed, since ``parse_skill`` refuses any other, so the first one that
    parses is the one ``load_skills`` would have kept.
    """
    top = _skill_dir(root, name)
    for skill_dir in _skill_dirs(root)[0]:
        if skill_dir.name != name:
            continue
        try:
            skill = parse_skill(skill_dir)
        except SkillError:
            continue
        return _loaded_dir(root, skill)
    if (
        top.is_dir()
        and not os.path.lexists(top / SKILL_FILE)
        and _source_skill_dirs(top)
    ):
        raise SkillConflict(f"{name} is a folder of skills, not a skill.")
    return top


def _write_new_file(path: Path, data: bytes) -> None:
    """Create ``path`` as a new regular file. A symlink is left untouched."""
    if path.is_symlink():
        raise SkillError("refusing to follow a symlink")
    if path.exists():
        path.unlink()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise SkillError("refusing to follow a symlink") from exc
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ManagedSkills:
    """The operator root sits under the declared directory (``VIGIL_INTENT_DIR``)."""

    directory: str
    revision: str


def managed_skills(settings: Optional[Settings] = None) -> Optional[ManagedSkills]:
    """Set when ``VIGIL_SKILLS_PATH`` is inside the declared directory (#5070).

    Those skills change by commit to that directory, never through Vigil; the
    revision is read from its ``REVISION`` marker on every call.
    """
    settings = settings or get_settings()
    declared, root = declared_dir(settings), operator_skills_root(settings)
    if declared is None or root is None:
        return None
    declared = declared.expanduser().resolve()
    if not root.resolve().is_relative_to(declared):
        return None
    try:
        revision = read_revision(declared)[1]
    except OSError:
        revision = "unknown"
    return ManagedSkills(str(declared), revision)


def _require_operator_root(settings: Optional[Settings]) -> Path:
    settings = settings or get_settings()
    root = operator_skills_root(settings)
    if root is None:
        raise SkillError("Operator skills are off (VIGIL_SKILLS_PATH is empty)")
    # Every Vigil write into a declared directory is refused: it changes by commit.
    managed = managed_skills(settings)
    if managed is not None:
        raise SkillError(
            f"Skills are managed by {managed.directory} at revision "
            f"{managed.revision}; change them by committing there"
        )
    # Only the default is created, private like the State Directory it sits in.
    # A path the operator named that is missing is more likely a volume that
    # did not mount than a folder to make.
    if settings.vigil_skills_path is None:
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise SkillError(f"could not create {root}: {exc}") from exc
    if not root.is_dir():
        raise SkillError(f"{root} is not a directory")
    return root.resolve()


# Public names for core.skills.skill_sources, which installs from a git repo.
writable_operator_root = _require_operator_root
repo_skill_dirs = _source_skill_dirs
inside = _inside


def _require_room(root: Path, adding: int = 1) -> None:
    """Refuse ``adding`` new skills that would take ``root`` past ``OPERATOR_SKILLS_MAX``.

    A skill folder is one holding ``SKILL.md``, at the top or inside a
    category; categories and empty folders do not count. Dot folders are
    skipped: they are the scratch copies a write leaves only while it runs.
    """
    held = len(_skill_dirs(root)[0])
    if held >= OPERATOR_SKILLS_MAX:
        raise SkillConflict(
            f"There are already {OPERATOR_SKILLS_MAX} custom skills, the most "
            "allowed. Delete one before adding another."
        )
    if held + adding > OPERATOR_SKILLS_MAX:
        raise SkillConflict(
            f"The upload holds {adding} skills but there is room for "
            f"{OPERATOR_SKILLS_MAX - held} more (the most allowed is "
            f"{OPERATOR_SKILLS_MAX}). Nothing was installed."
        )


def _accepts_skill(name: str, content: str) -> None:
    """Parse a rendered file in a throwaway directory before touching the root."""
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = _direct_child(Path(tmp), name)
        try:
            skill_dir.mkdir()
        except OSError as exc:
            raise SkillError(f"invalid skill name {name!r}") from exc
        (skill_dir / SKILL_FILE).write_text(content, encoding="utf-8")
        parse_skill(skill_dir)


def _copy_skill_dir(source: Path, dest: Path) -> None:
    """Copy ``source`` to ``dest``, leaving behind what ``skill_files`` does not list."""

    def skip_hidden(directory: str, names: List[str]) -> List[str]:
        return [
            n for n in names if n.startswith(".") or Path(directory, n).is_symlink()
        ]

    # copyfile drops the source's modes, so a read-only library still copies to a writable folder
    shutil.copytree(
        source, dest, symlinks=True, ignore=skip_hidden, copy_function=shutil.copyfile
    )
    for directory, _, _ in os.walk(dest):
        os.chmod(directory, 0o755)  # nosec B103 - skill dirs are world-readable


def write_operator_skill(
    name: str,
    description: str,
    body: str,
    settings: Optional[Settings] = None,
    source: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> Skill:
    """Write ``<root>/[<category>/...]<name>/SKILL.md`` that ``parse_skill`` accepts.

    A name the bundled library already owns is refused: ``load_skills`` would
    skip the operator copy, so the file would be invisible. An existing
    operator skill, top-level or in a category, is overwritten in its own
    folder: only SKILL.md changes, its other frontmatter keys carry over and
    ``metadata.version`` goes up by one.
    With ``source`` (a loaded skill's name) and no skill of this name yet, the
    source's whole folder is copied first and the copy starts at version 1.
    An overwrite must send the ``expected_version`` it opened: a folder that
    exists with none sent, or with a different one on disk, raises
    :class:`SkillConflict` before anything is written. Check and write are not
    atomic. ``expected_version`` is ignored when copying.
    """
    root = _require_operator_root(settings)
    skill_dir = _operator_skill_dir(root, name)
    if name in _library_names():
        raise SkillError(
            f"name {name!r} belongs to the bundled library; save it under a new name"
        )
    if not skill_dir.exists():
        _require_room(root)
    origin: Optional[Skill] = None
    if source is not None:
        if skill_dir.exists():
            raise SkillError(f"a skill named {name!r} already exists")
        origin = {s.name: s for s in load_skills(skill_roots(settings))}.get(source)
        if origin is None:
            raise SkillNotFound(f"No skill named {source!r}")
    if origin is not None:
        existing, version = _frontmatter_of(origin.path), 1
    elif skill_dir.exists():
        if expected_version is None:
            raise SkillConflict(f"A skill folder named {name} already exists.")
        existing = _frontmatter_of(skill_dir)
        if expected_version != _version_of(existing):
            raise SkillConflict(
                "This skill changed since you opened it. Reopen it to see the latest."
            )
        version = expected_version + 1
    else:
        existing, version = {}, 1
    metadata = existing.get("metadata")
    extra = {
        **existing,
        "metadata": {
            **(metadata if isinstance(metadata, dict) else {}),
            "version": str(version),
        },
    }
    content = render_skill_markdown(name, description, body, extra)
    _accepts_skill(name, content)
    if origin is not None:
        _install_copy(root, origin, name, content)
        return parse_skill(skill_dir)
    try:
        skill_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise SkillError(f"could not create {skill_dir}: {exc}") from exc
    target = skill_dir / SKILL_FILE
    tmp = skill_dir / ".SKILL.md.write"
    _write_new_file(tmp, content.encode("utf-8"))
    os.replace(tmp, target)
    return parse_skill(skill_dir)


def _install_copy(root: Path, origin: Skill, name: str, content: str) -> None:
    """Build the copy in a temp dir under ``root``, then move it into place."""
    try:
        scratch = Path(tempfile.mkdtemp(prefix=".copy-", dir=root))
    except OSError as exc:
        raise SkillError(f"could not write under {root}: {exc}") from exc
    try:
        staged = _direct_child(scratch, name)
        final = _skill_dir(root, name)
        _copy_skill_dir(origin.path, staged)
        _write_new_file(staged / SKILL_FILE, content.encode("utf-8"))
        parse_skill(staged)
        os.replace(staged, final)
    except OSError as exc:
        raise SkillError(f"could not copy {origin.name!r}: {exc}") from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _zip_files(zf: zipfile.ZipFile) -> Dict[str, zipfile.ZipInfo]:
    """The zip's regular files, by path, judged from the entry list alone.

    An unsafe entry refuses the whole zip, even one that would be skipped.
    ``.`` path parts, as tar-to-zip tools write them, are dropped from names.
    Directories, ``.git`` and ``__MACOSX`` are dropped. Other dot paths are
    kept for finding skills (``.claude/skills``, ``.claude-plugin``); a
    skill's own dotfiles are dropped when it is unpacked. The file count and
    size caps apply to the skills installed (``_unpack``), so a repo's docs
    beside them do not count; ``_ZIP_ENTRIES_MAX`` bounds the search.
    """
    kept: Dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        raw = info.filename
        parts = [p for p in raw.rstrip("/").split("/") if p != "."]
        name = "/".join(parts)
        if not parts and info.is_dir():  # "./" itself
            continue
        if (
            not parts
            or "\\" in raw
            or "\0" in raw
            or raw.startswith("/")
            or re.match(r"^[A-Za-z]:", raw)
            or ".." in parts
            or "" in parts  # "a//x" would rejoin as the absolute "/x"
        ):
            raise SkillError(f"the zip has an unsafe path: {raw!r}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SkillError(f"the zip has a symlink: {raw!r}")
        if info.is_dir():
            continue
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise SkillError(f"the zip has a non-regular file: {raw!r}")
        if any(p in (".git", "__MACOSX") for p in parts):
            continue
        if name in kept:
            raise SkillError(f"the zip lists {name!r} twice")
        kept[name] = info
        if len(kept) > _ZIP_ENTRIES_MAX:
            raise SkillError(f"the zip has more than {_ZIP_ENTRIES_MAX} files")
    return kept


def skipped_note(skipped: int) -> Optional[str]:
    """One short clause saying how many ``SKILL.md`` a zip upload skipped and why."""
    if not skipped:
        return None
    files = "file was" if skipped == 1 else "files were"
    return (
        f"{skipped} {SKILL_FILE} {files} skipped: not where npx skills looks, "
        "or a skill name already taken in the zip"
    )


_MANIFESTS = ("skills.sh.json", "plugin.json", "marketplace.json")
_OUTLINE_READ = 64 * 1024  # of each SKILL.md and manifest, for the outline
_ZIP_ENTRIES_MAX = 5000  # files in a zip at all; each is an empty file in the outline


def _skill_folders(
    zf: zipfile.ZipFile, kept: Dict[str, zipfile.ZipInfo]
) -> tuple[List[str], int]:
    """The zip folders holding a skill, ``""`` for the root, and how many
    ``SKILL.md`` were skipped.

    Found as ``npx skills`` finds skills in a repo (``_source_skill_dirs``),
    the repo being the zip's root, or its one top-level folder when that is not
    itself a skill (a GitHub "download zip" wrapper). A ``SKILL.md`` at that
    root is the one skill. Of two skill folders with one name the first found
    wins. The search runs on an outline of the zip on disk: its paths as empty
    files, except each ``SKILL.md`` (the fallback search needs to know whether
    one is valid) and the manifests that point at skills.
    A skill folder inside another one is refused: it would otherwise be stored
    as a supporting file of the outer skill and never load.
    """
    every = [
        p[: -len(SKILL_FILE)].rstrip("/")
        for p in kept
        if p == SKILL_FILE or p.endswith(f"/{SKILL_FILE}")
    ]
    if not every:
        raise SkillError(f"no {SKILL_FILE} in the zip")
    try:
        outline = tempfile.TemporaryDirectory(prefix="vigil-zip-")
    except OSError as exc:
        raise SkillError(f"could not search the zip: {exc.strerror}") from exc
    with outline as tmp:
        tree = Path(tmp)
        try:
            for name, info in kept.items():
                path = tree / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                if path.name == SKILL_FILE or path.name in _MANIFESTS:
                    with zf.open(info) as handle:
                        path.write_bytes(handle.read(_OUTLINE_READ))
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTDIR, errno.EISDIR):
                raise SkillError("the zip has a file and a folder at one path") from exc
            raise SkillError(f"could not search the zip: {exc.strerror}") from exc
        top = tree
        children = list(tree.iterdir())
        if (
            len(children) == 1
            and children[0].is_dir()
            and not (children[0] / SKILL_FILE).exists()
        ):
            top = children[0]
        if (top / SKILL_FILE).exists():
            dirs = [top]
        else:
            dirs = _source_skill_dirs(top)
        folders: List[str] = []
        for d in dirs:
            rel = d.relative_to(tree).as_posix()
            rel = "" if rel == "." else rel
            if all(rel.rsplit("/", 1)[-1] != f.rsplit("/", 1)[-1] for f in folders):
                folders.append(rel)
    for inner in every:
        for outer in folders:
            if inner != outer and (outer == "" or inner.startswith(f"{outer}/")):
                where = (
                    f"inside the skill folder {outer!r}"
                    if outer
                    else "below a root SKILL.md"
                )
                raise SkillError(
                    f"{inner!r} is a skill folder {where}; a skill cannot hold another skill"
                )
    skipped = len(every) - len(folders)
    if not folders:
        raise SkillError(f"no skill to install: {skipped_note(skipped)}")
    return folders, skipped


def _unpack(
    zf: zipfile.ZipFile,
) -> tuple[List[tuple[Optional[str], Dict[str, bytes]]], int]:
    """Each skill in the zip as ``(folder, {path within it: bytes})``, and how
    many ``SKILL.md`` were skipped.

    ``folder`` is the skill folder's own name, or None for a SKILL.md at the
    zip's root, which takes every file. Files outside any skill folder, such as
    a README beside several skills, and a skill's own dotfiles are left out.
    """
    kept = _zip_files(zf)
    folders, skipped = _skill_folders(zf, kept)
    chosen: List[tuple[Optional[str], Dict[str, zipfile.ZipInfo]]] = []
    for folder in folders:
        prefix = f"{folder}/" if folder else ""
        infos = {
            name[len(prefix) :]: info
            for name, info in kept.items()
            if name.startswith(prefix)
            and not any(p.startswith(".") for p in name[len(prefix) :].split("/"))
        }
        chosen.append((folder.rsplit("/", 1)[-1] if folder else None, infos))
    count = sum(len(infos) for _, infos in chosen)
    if count > _FILES_MAX:
        raise SkillError(f"the skills in the zip hold more than {_FILES_MAX} files")
    if sum(i.file_size for _, infos in chosen for i in infos.values()) > _UNPACKED_MAX:
        raise SkillError(f"the zip unpacks to more than {_UNPACKED_MAX // 1024} KB")
    budget = _UNPACKED_MAX  # the headers' sizes can lie; bound what is really read
    skills: List[tuple[Optional[str], Dict[str, bytes]]] = []
    for skill_folder, infos in chosen:
        files: Dict[str, bytes] = {}
        for rel, info in infos.items():
            with zf.open(info) as handle:
                data = handle.read(budget + 1)
            budget -= len(data)
            if budget < 0:
                raise SkillError(
                    f"the zip unpacks to more than {_UNPACKED_MAX // 1024} KB"
                )
            files[rel] = data
        skills.append((skill_folder, files))
    return skills, skipped


def folder_files(folder: Path) -> Dict[str, bytes]:
    """A skill folder on disk as ``{path within it: bytes}``, for install.

    The same files an upload keeps: dotfiles and dot-folders are left out, and
    so is anything that is not a regular file, a symlink above all, so nothing
    outside the folder is read. Bounded as an upload is.
    """
    files: Dict[str, bytes] = {}
    budget = _UNPACKED_MAX
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            if len(files) >= _FILES_MAX:
                raise SkillError(f"it holds more than {_FILES_MAX} files")
            with path.open("rb") as handle:
                data = handle.read(budget + 1)
            budget -= len(data)
            if budget < 0:
                raise SkillError(f"it is larger than {_UNPACKED_MAX // 1024} KB")
            files[path.relative_to(folder).as_posix()] = data
    return files


def _declared_name(skill_md: bytes) -> str:
    """The frontmatter ``name`` when it can name a folder, else a placeholder.

    A bad name still goes through ``parse_skill`` under the placeholder, so the
    refusal carries the loader's own wording.
    """
    try:
        frontmatter, _ = split_frontmatter(skill_md.decode("utf-8-sig"))
    except (FrontmatterError, UnicodeDecodeError) as exc:
        raise SkillError(str(exc)) from exc
    name = (frontmatter or {}).get("name")
    ok = isinstance(name, str) and _NAME_RE.match(name) and len(name) <= _NAME_MAX
    return name if ok else "invalid-name"


def _stage_upload(
    scratch: Path,
    root: Path,
    library: set[str],
    name: str,
    files: Dict[str, bytes],
    replacing: bool = False,
) -> tuple[Path, Path]:
    """Write one skill under ``scratch``; return it and where it goes.

    A new skill's name must be free. One ``replacing`` must be an operator
    skill already, and goes where that one loaded from.
    """
    staged = scratch / name
    try:
        staged.mkdir()
        for rel, content in files.items():
            target = staged / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_new_file(target, content)
    except OSError as exc:
        raise SkillError(f"could not install {name!r}: {exc}") from exc
    skill = parse_skill(staged)
    if skill.name in library:
        raise SkillConflict(
            f"name {skill.name!r} belongs to the bundled library; choose another name"
        )
    if replacing:
        final = _operator_skill_dir(root, skill.name)
        if not (final / SKILL_FILE).is_file():
            raise SkillNotFound(f"No skill named {skill.name!r} to replace")
        return staged, final
    final = _skill_dir(root, skill.name)
    if os.path.lexists(final) or _operator_skill_dir(root, skill.name) != final:
        raise SkillConflict(
            f"A skill named {skill.name!r} already exists. Delete it first, then upload again."
        )
    return staged, final


def _refusal(failures: List[tuple[str, SkillError]], total: int) -> SkillError:
    """One error for every skill refused; a lone skill keeps its own message."""
    if total == 1:
        return failures[0][1]
    detail = "; ".join(f"{name!r}: {exc}" for name, exc in failures[:5])
    if len(failures) > 5:
        detail += f"; and {len(failures) - 5} more"
    conflict = all(isinstance(exc, SkillConflict) for _, exc in failures)
    return (SkillConflict if conflict else SkillError)(
        f"Nothing was installed: {len(failures)} of the {total} skills "
        f"failed. {detail}"
    )


def install_uploaded_skills(
    filename: str, data: bytes, settings: Optional[Settings] = None
) -> tuple[List[Skill], int]:
    """Install an uploaded ``SKILL.md`` or ``.zip`` of skills under the operator root.

    Returns the skills installed and how many ``SKILL.md`` in the zip were
    skipped. A bare file lands in a folder named by its frontmatter ``name``.
    A zip holds ``SKILL.md`` at its root (named the same way), or skill
    folders named for their skills, found where ``npx skills`` looks in a repo
    (``_skill_folders``). Nothing is overwritten: a name the library or the
    operator root already holds raises :class:`SkillConflict`. All or nothing:
    every skill is built and checked in a scratch directory under the root
    before any moves into place, and a skill already moved is taken out again
    if a later move fails, so a refused upload leaves nothing behind.
    """
    root = _require_operator_root(settings)
    if len(data) > UPLOAD_MAX:
        raise SkillError(f"the file is larger than {UPLOAD_MAX // 1024} KB")
    if filename.lower().endswith(".zip") or data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                unpacked, skipped = _unpack(zf)
        except (
            zipfile.BadZipFile,
            RuntimeError,
            NotImplementedError,
            zlib.error,
            EOFError,
            OSError,
            lzma.LZMAError,
        ) as exc:
            raise SkillError(f"not a readable zip archive: {exc}") from exc
    else:
        unpacked, skipped = [(None, {SKILL_FILE: data})], 0
    named = [
        (folder or _declared_name(files[SKILL_FILE]), files)
        for folder, files in unpacked
    ]
    names = [name for name, _ in named]
    for name in names:
        if names.count(name) > 1:
            raise SkillError(f"the zip holds more than one skill folder named {name!r}")
    return install_skill_files(root, named), skipped


def install_skill_files(
    root: Path,
    named: List[tuple[str, Dict[str, bytes]]],
    replace: Collection[str] = (),
    freeing: int = 0,
) -> List[Skill]:
    """Install skills given as ``(folder name, {path within it: bytes})``.

    Nothing is overwritten except the names in ``replace``, which must be
    operator skills already and are swapped for the new files where they live;
    any other name the library or the operator root holds raises
    :class:`SkillConflict`. All or nothing: every skill is built and checked in
    a scratch directory under the root before any moves into place, and if a
    later move fails each one already made is undone, a replaced skill put back.
    ``freeing`` counts skills the caller deletes right after, so the cap on
    custom skills is checked against what is left, and a replace alone never
    hits it.
    """
    growth = sum(1 for name, _ in named if name not in replace) - freeing
    if growth > 0:
        _require_room(root, growth)
    try:
        scratch = Path(tempfile.mkdtemp(prefix=".copy-", dir=root))
    except OSError as exc:
        raise SkillError(f"could not write under {root}: {exc}") from exc
    library = _library_names()
    moved: List[Path] = []  # new skills now in place
    swapped: List[tuple[Path, Path]] = []  # (a replaced skill's place, its old copy)
    installed = False
    try:
        staged: List[tuple[Path, Path, bool]] = []
        failures: List[tuple[str, SkillError]] = []
        for name, files in named:
            replacing = name in replace
            try:
                path, final = _stage_upload(
                    scratch, root, library, name, files, replacing
                )
                staged.append((path, final, replacing))
            except SkillError as exc:
                failures.append((name, exc))
        if failures:
            raise _refusal(failures, len(named))
        for path, final, replacing in staged:
            try:
                if replacing:
                    old = scratch / f".old-{path.name}"
                    os.replace(final, old)
                    swapped.append((final, old))
                elif os.path.lexists(final):
                    raise FileExistsError(final)
                os.replace(path, final)
            except OSError as exc:
                if not replacing and os.path.lexists(final):  # lost a race
                    raise SkillConflict(
                        f"A skill named {path.name!r} already exists."
                    ) from exc
                raise SkillError(f"could not install {path.name!r}: {exc}") from exc
            moved.append(final)
        installed = True
    finally:
        keep = False  # a backup that could not be put back is left in scratch
        if not installed:
            replaced = {final for final, _ in swapped}
            for final in moved:
                if final not in replaced:
                    shutil.rmtree(final, ignore_errors=True)
            for final, old in swapped:
                shutil.rmtree(final, ignore_errors=True)
                try:
                    os.replace(old, final)
                except OSError:
                    keep = True
                    logger.exception("could not put %s back; it is at %s", final, old)
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)
    return [parse_skill(final) for final in moved]


def delete_operator_skill(name: str, settings: Optional[Settings] = None) -> None:
    """Remove an operator skill directory. Bundled skills are refused.

    A skill inside other folders goes, and so does each folder above it that
    is left empty, up to the root: an empty top-level folder would otherwise
    be logged on every load.
    """
    root = _require_operator_root(settings)
    # The resolved root, so a loaded path lines up with it for _loaded_dir.
    skill = {item.name: item for item in load_skills([LIBRARY_ROOT, root])}.get(name)
    if skill is None:
        raise SkillNotFound(f"No skill named {name!r}")
    if is_bundled(skill):
        raise SkillError("bundled skills cannot be deleted")
    if skill.path.is_symlink():
        raise SkillError("refusing to follow a symlink")
    target = _loaded_dir(root, skill)
    if not _contained(root, target):
        raise SkillError("refusing to delete a skill outside the operator root")
    library = LIBRARY_ROOT.resolve()
    if target == library or target.is_relative_to(library):
        raise SkillError("refusing to delete a bundled skill")
    shutil.rmtree(target)
    parent = target.parent
    while parent != root:
        try:
            parent.rmdir()  # only succeeds when the folder is empty
        except OSError:
            break
        parent = parent.parent


def _confined(skill_dir: Path, file: str) -> Optional[Path]:
    """``skill_dir/file`` resolved, or None when it would leave the directory.

    Absolute paths and ``..`` are refused before touching the filesystem;
    resolving afterwards catches a symlink that points outside.
    """
    if PurePosixPath(file).is_absolute() or PureWindowsPath(file).is_absolute():
        return None
    if ".." in PurePosixPath(file).parts or not file.strip():
        return None
    base = skill_dir.resolve()
    target = (base / file).resolve()
    if not target.is_relative_to(base):
        return None
    return target


def read_skill(
    name: Any,
    file: Any = None,
    roots: Optional[Iterable[Path]] = None,
    skills: Optional[Iterable[Skill]] = None,
) -> Dict[str, Any]:
    """The tool body. Without ``file``, the SKILL.md body; with it, that file.

    Answers ``{"error": ...}`` rather than raising, as ``_replay_hunt`` does: a
    model that asked for a file the skill does not carry needs to be told so.
    Nothing is executed: a script under ``scripts/`` is returned as text like
    any other file. ``skills``, when given, is the whole set that can be read,
    instead of what ``roots`` loads: the agent tool passes the skills that are
    not held.
    """
    if not isinstance(name, str) or not name:
        return {"error": "read_skill requires a skill `name`"}
    if skills is None:
        skills = load_skills(skill_roots() if roots is None else roots)
    skill = {s.name: s for s in skills}.get(name)
    if skill is None:
        return {"error": f"No skill named {name!r}"}
    if file is not None and not isinstance(file, str):
        return {"error": "`file` must be a path relative to the skill directory"}
    # A NUL byte or an over-long name raises from the path layer itself; those
    # are the model's mistakes to hear about, not the tool's to crash on.
    try:
        return _read(skill, file or None)
    except (OSError, ValueError) as exc:
        return {"error": f"Could not read {file or SKILL_FILE!r}: {exc}"}


def read_skill_file(skill: Skill, file: str) -> str:
    """The text of ``file`` inside the skill. Raises :class:`SkillError` otherwise."""
    target = _confined(skill.path, file)
    if target is None:
        raise SkillError(f"{file!r} is outside skill {skill.name!r}")
    if not target.is_file():
        raise SkillNotFound(f"Skill {skill.name!r} has no file {file!r}")
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{file!r} is not a text file") from exc


def _read(skill: Skill, file: Optional[str]) -> Dict[str, Any]:
    if file is None:
        return {"skill": skill.name, "file": SKILL_FILE, "content": skill_body(skill)}
    try:
        return {
            "skill": skill.name,
            "file": file,
            "content": read_skill_file(skill, file),
        }
    except SkillError as exc:
        return {"error": str(exc)}
