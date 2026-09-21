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

import logging
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, Iterable, List, Optional

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


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the skill directory; SKILL.md and references/ live under it


class SkillError(ValueError):
    """The directory is not a spec-conformant skill."""


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
    if settings.vigil_skills_path:
        roots.append(Path(settings.vigil_skills_path).expanduser())
    return roots


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


def _read(skill: Skill, file: Optional[str]) -> Dict[str, Any]:
    if file is None:
        content = (skill.path / SKILL_FILE).read_text(encoding="utf-8-sig")
        _, offset = split_frontmatter(content)
        body = content[offset:].lstrip("\n")
        return {"skill": skill.name, "file": SKILL_FILE, "content": body}
    target = _confined(skill.path, file)
    if target is None:
        return {"error": f"{file!r} is outside skill {skill.name!r}"}
    if not target.is_file():
        return {"error": f"Skill {skill.name!r} has no file {file!r}"}
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": f"{file!r} is not a text file"}
    return {"skill": skill.name, "file": file, "content": text}
