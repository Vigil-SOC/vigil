"""Import Claude Desktop-compatible skill ``.zip`` bundles (Issue #130).

A Claude Desktop skill ships as a zip containing ``SKILL.md`` — YAML
frontmatter plus a markdown body. The frontmatter maps 1:1 to the
``skills`` table (``name``, ``description``, ``category``,
``input_schema``, ``output_schema``, ``required_tools``) and the body
becomes ``prompt_template``.

This module parses the zip, normalizes fields, and upserts via
:class:`SkillService`. A name collision updates the existing row and
bumps its version; a fresh name creates a new row.

v1 scope: only ``SKILL.md`` is accepted. Any other zip entry is
rejected with ``400`` and the offending paths listed in ``details``.
Attachment storage is left to a follow-up.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Any, Dict, List, Optional

import yaml

from services import skill_service as _skill_service_mod

logger = logging.getLogger(__name__)


MAX_ZIP_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 32
MAX_SKILL_MD_BYTES = 1 * 1024 * 1024

ALLOWED_CATEGORIES = {
    "detection",
    "enrichment",
    "response",
    "reporting",
    "custom",
}

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<front>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


class SkillImportError(Exception):
    """Validation failure during import. Carries an HTTP status code."""

    def __init__(
        self,
        status_code: int,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details or {}


def import_skill_zip(
    zip_bytes: bytes,
    created_by: Optional[str] = None,
    service: Optional[Any] = None,
) -> Dict[str, Any]:
    """Parse a Claude Desktop skill zip and upsert into the ``skills`` table.

    Returns a dict with ``skill_id``, ``name``, ``version``, ``replaced``.
    Raises :class:`SkillImportError` on any validation failure.
    """
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise SkillImportError(
            413,
            f"Zip exceeds {MAX_ZIP_BYTES // (1024 * 1024)} MB limit",
            {"size_bytes": len(zip_bytes), "limit_bytes": MAX_ZIP_BYTES},
        )

    skill_md_text = _extract_skill_md(zip_bytes)
    name, patch = _parse_skill_md(skill_md_text)

    svc = service or _skill_service_mod.SkillService()
    existing = _find_by_name(svc, name)

    if existing is not None:
        updated = svc.update_skill(existing["skill_id"], patch)
        if not updated:
            raise SkillImportError(
                500,
                "Failed to update existing skill",
                {"skill_id": existing["skill_id"]},
            )
        return {
            "skill_id": updated["skill_id"],
            "name": updated["name"],
            "version": updated["version"],
            "replaced": True,
        }

    created = svc.create_skill(patch, created_by=created_by)
    return {
        "skill_id": created["skill_id"],
        "name": created["name"],
        "version": created["version"],
        "replaced": False,
    }


def _extract_skill_md(zip_bytes: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillImportError(400, f"Not a valid zip file: {exc}")

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise SkillImportError(
                400,
                f"Zip has more than {MAX_ENTRIES} entries",
                {"entry_count": len(infos), "limit": MAX_ENTRIES},
            )

        top_level = _detect_top_level_prefix(infos)
        skill_md_name = f"{top_level}SKILL.md" if top_level else "SKILL.md"

        rejected: List[str] = []
        skill_md_info: Optional[zipfile.ZipInfo] = None
        for info in infos:
            if info.filename == skill_md_name:
                skill_md_info = info
            else:
                rejected.append(info.filename)

        if skill_md_info is None:
            raise SkillImportError(
                400,
                (
                    "Zip must contain a SKILL.md at the root or under a "
                    "single top-level folder"
                ),
                {"entries": [i.filename for i in infos]},
            )

        if rejected:
            raise SkillImportError(
                400,
                (
                    "Zip contains files other than SKILL.md; attachments "
                    "are not supported in v1"
                ),
                {"rejected_paths": rejected},
            )

        if skill_md_info.file_size > MAX_SKILL_MD_BYTES:
            raise SkillImportError(
                400,
                f"SKILL.md exceeds {MAX_SKILL_MD_BYTES // (1024 * 1024)} MB limit",
                {
                    "size_bytes": skill_md_info.file_size,
                    "limit_bytes": MAX_SKILL_MD_BYTES,
                },
            )

        raw = zf.read(skill_md_info)

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _detect_top_level_prefix(infos: List[zipfile.ZipInfo]) -> str:
    """Return a single top-level directory prefix (with trailing ``/``) or ``""``.

    Claude Desktop bundles sometimes nest SKILL.md under a single folder
    named after the skill. If every entry shares the same first path
    segment, treat that segment as the prefix.
    """
    prefixes = set()
    for info in infos:
        head, sep, _ = info.filename.partition("/")
        if sep:
            prefixes.add(head + "/")
        else:
            return ""
    if len(prefixes) == 1:
        return next(iter(prefixes))
    return ""


def _parse_skill_md(text: str) -> tuple[str, Dict[str, Any]]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillImportError(
            400,
            "SKILL.md must begin with YAML frontmatter delimited by '---' lines",
        )

    try:
        front = yaml.safe_load(match.group("front")) or {}
    except yaml.YAMLError as exc:
        raise SkillImportError(400, f"Invalid YAML in SKILL.md frontmatter: {exc}")

    if not isinstance(front, dict):
        raise SkillImportError(
            400,
            "SKILL.md frontmatter must be a YAML mapping",
        )

    body = match.group("body").strip()
    if not body:
        raise SkillImportError(
            400,
            "SKILL.md body is empty; it becomes the prompt_template and is required",
        )

    missing = [k for k in ("name", "category") if not front.get(k)]
    if missing:
        raise SkillImportError(
            400,
            "SKILL.md frontmatter is missing required fields",
            {"missing_fields": missing},
        )

    name = str(front["name"]).strip()
    category = str(front["category"]).strip().lower()
    if category not in ALLOWED_CATEGORIES:
        raise SkillImportError(
            400,
            f"Unknown category '{category}'",
            {"allowed": sorted(ALLOWED_CATEGORIES)},
        )

    required_tools = _coerce_required_tools(front.get("required_tools"))
    input_schema = _coerce_mapping(front.get("input_schema"), "input_schema")
    output_schema = _coerce_mapping(front.get("output_schema"), "output_schema")

    description = front.get("description")
    if description is not None and not isinstance(description, str):
        description = str(description)

    patch = {
        "name": name,
        "description": description,
        "category": category,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "required_tools": required_tools,
        "prompt_template": body,
        "execution_steps": [],
        "is_active": True,
    }
    return name, patch


def _coerce_required_tools(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise SkillImportError(
                    400,
                    "required_tools must be a list of strings",
                    {"offending_value": repr(item)},
                )
            out.append(item)
        return out
    raise SkillImportError(
        400,
        "required_tools must be a string or a list of strings",
        {"got_type": type(value).__name__},
    )


def _coerce_mapping(value: Any, field: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    raise SkillImportError(
        400,
        f"{field} must be a YAML mapping",
        {"got_type": type(value).__name__},
    )


def _find_by_name(service: Any, name: str) -> Optional[Dict[str, Any]]:
    for row in service.list_skills():
        if row.get("name") == name:
            return row
    return None
