"""Skills as directories of ``SKILL.md`` (epic #882, #925).

A skill is ``<root>/<name>/SKILL.md`` conforming to the public Agent Skills
spec (agentskills.io/specification): YAML frontmatter naming the skill and a
Markdown body telling the agent how to do the thing. Roots are the bundled
``core/skills/library/`` plus one optional directory from ``Settings``.

No database, no module state: ``load_skills`` takes its roots so tests can
point it at ``tmp_path``. An invalid skill is logged and skipped so one bad
directory cannot take the library down with it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Optional

import yaml

from core.config import Settings, get_settings
from core.frontmatter import FrontmatterError, split_frontmatter

logger = logging.getLogger(__name__)

READ_SKILL_TOOL = "read_skill"
SKILL_FILE = "SKILL.md"
LIBRARY_ROOT = Path(__file__).resolve().parent / "library"

# The spec's name grammar: lowercase letters, digits and single hyphens, never
# at either end. The length bound is checked separately for a clearer message.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME_MAX = 64
_DESCRIPTION_MAX = 1024
_COMPATIBILITY_MAX = 500
_FILES_MAX = 200


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
    """The write clashes with what is on disk: a stale version or a taken folder."""


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


def parse_skill(skill_dir: Path) -> Skill:
    """Validate ``skill_dir/SKILL.md`` against the spec and return the skill.

    Raises :class:`SkillError` for anything the spec forbids. Keys the spec does
    not name are left alone: rejecting them would make the loader stricter than
    the format it claims to read.
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
    if name != skill_dir.name:
        raise SkillError(f"`name` {name!r} does not match directory {skill_dir.name!r}")
    description = _require_str(frontmatter, "description", _DESCRIPTION_MAX)
    _check_optional(frontmatter)
    return Skill(name=name, description=description, path=skill_dir)


def as_user_turn(user_input: Any) -> str:
    """The user turn an eval sends: a string as itself, otherwise indented JSON."""
    return (
        user_input if isinstance(user_input, str) else json.dumps(user_input, indent=2)
    )


def load_skills(roots: Iterable[Path]) -> List[Skill]:
    """Every valid skill under ``roots``, in root then name order.

    Roots earlier in the list win a name clash, so the bundled library cannot be
    shadowed by the setting root. A missing root is not an error: the bundled
    directory may be empty and the setting root is optional.
    """
    loaded: Dict[str, Skill] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            logger.debug("Skills root %s is not a directory; skipping", root)
            continue
        for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
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
    """The bundled library, then the optional ``VIGIL_SKILLS_PATH`` root."""
    settings = settings or get_settings()
    roots = [LIBRARY_ROOT]
    operator = operator_skills_root(settings)
    if operator is not None:
        roots.append(operator)
    return roots


def operator_skills_root(settings: Optional[Settings] = None) -> Optional[Path]:
    """The operator skills directory, or None when ``VIGIL_SKILLS_PATH`` is unset.

    An unset path is not created.
    """
    settings = settings or get_settings()
    raw = (settings.vigil_skills_path or "").strip()
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


def _require_operator_root(settings: Optional[Settings]) -> Path:
    root = operator_skills_root(settings)
    if root is None:
        raise SkillError("The skills path is unset")
    if not root.is_dir():
        raise SkillError(f"{root} is not a directory")
    return root.resolve()


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
    """Write ``<vigil_skills_path>/<name>/SKILL.md`` that ``parse_skill`` accepts.

    A name the bundled library already owns is refused: ``load_skills`` would
    skip the operator copy, so the file would be invisible. An existing
    operator skill is overwritten in place: only SKILL.md changes, its other
    frontmatter keys carry over and ``metadata.version`` goes up by one.
    With ``source`` (a loaded skill's name) and no skill of this name yet, the
    source's whole folder is copied first and the copy starts at version 1.
    An overwrite must send the ``expected_version`` it opened: a folder that
    exists with none sent, or with a different one on disk, raises
    :class:`SkillConflict` before anything is written. Check and write are not
    atomic. ``expected_version`` is ignored when copying.
    """
    root = _require_operator_root(settings)
    skill_dir = _skill_dir(root, name)
    if name in _library_names():
        raise SkillError(
            f"name {name!r} belongs to the bundled library; save it under a new name"
        )
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


def delete_operator_skill(name: str, settings: Optional[Settings] = None) -> None:
    """Remove an operator skill directory. Bundled skills are refused."""
    root = _require_operator_root(settings)
    skill = {item.name: item for item in load_skills(skill_roots(settings))}.get(name)
    if skill is None:
        raise SkillNotFound(f"No skill named {name!r}")
    if is_bundled(skill):
        raise SkillError("bundled skills cannot be deleted")
    if skill.path.is_symlink():
        raise SkillError("refusing to follow a symlink")
    target = skill.path.resolve()
    if (
        target.parent != root
        or target.name != skill.name
        or not _contained(root, target)
    ):
        raise SkillError("refusing to delete a skill outside the operator root")
    library = LIBRARY_ROOT.resolve()
    if target == library or target.is_relative_to(library):
        raise SkillError("refusing to delete a bundled skill")
    shutil.rmtree(target)


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
    name: Any, file: Any = None, roots: Optional[Iterable[Path]] = None
) -> Dict[str, Any]:
    """The tool body. Without ``file``, the SKILL.md body; with it, that file.

    Answers ``{"error": ...}`` rather than raising, as ``_replay_hunt`` does: a
    model that asked for a file the skill does not carry needs to be told so.
    Nothing is executed: a script under ``scripts/`` is returned as text like
    any other file.
    """
    if not isinstance(name, str) or not name:
        return {"error": "read_skill requires a skill `name`"}
    skills = {s.name: s for s in load_skills(skill_roots() if roots is None else roots)}
    skill = skills.get(name)
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
