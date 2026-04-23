"""Unit tests for the Claude Desktop skill zip importer (Issue #130).

Exercises ``services.skill_importer.import_skill_zip`` against an
in-memory fake service. No DB or HTTP involved.
"""

import io
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.skill_importer import (  # noqa: E402
    MAX_ENTRIES,
    MAX_SKILL_MD_BYTES,
    MAX_ZIP_BYTES,
    SkillImportError,
    import_skill_zip,
)

# --------------------------------------------------------------------------- #
# In-memory fake SkillService matching the subset of the API the importer uses
# --------------------------------------------------------------------------- #


class FakeSkillService:
    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}
        self._next = 1

    def list_skills(
        self,
        category: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        return list(self._store.values())

    def create_skill(
        self,
        data: Dict[str, Any],
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        skill_id = f"s-20260423-{self._next:08X}"
        self._next += 1
        row = {
            "skill_id": skill_id,
            "version": 1,
            "created_by": created_by,
            **data,
        }
        self._store[skill_id] = row
        return row

    def update_skill(
        self,
        skill_id: str,
        patch: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        row = self._store.get(skill_id)
        if not row:
            return None
        content_fields = {
            "name",
            "description",
            "category",
            "input_schema",
            "output_schema",
            "required_tools",
            "prompt_template",
            "execution_steps",
        }
        bumped = False
        for k, v in patch.items():
            if v is None:
                continue
            if k in content_fields and row.get(k) != v:
                bumped = True
            row[k] = v
        if bumped:
            row["version"] = row.get("version", 1) + 1
        return row


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_zip(entries: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


VALID_SKILL_MD = b"""---
name: Detect Lateral RDP
description: Finds unusual RDP sessions.
category: detection
required_tools:
  - splunk.search
input_schema:
  type: object
  properties:
    hours:
      type: integer
---
Look for suspicious RDP logons in the last {{hours}} hours."""


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_happy_path_creates_new_skill():
    svc = FakeSkillService()
    zip_bytes = _make_zip({"SKILL.md": VALID_SKILL_MD})

    result = import_skill_zip(zip_bytes, created_by="alice", service=svc)

    assert result["replaced"] is False
    assert result["name"] == "Detect Lateral RDP"
    assert result["version"] == 1
    row = svc._store[result["skill_id"]]
    assert row["category"] == "detection"
    assert row["required_tools"] == ["splunk.search"]
    assert row["prompt_template"].startswith("Look for suspicious RDP")
    assert row["input_schema"]["properties"]["hours"]["type"] == "integer"
    assert row["created_by"] == "alice"


@pytest.mark.unit
def test_happy_path_accepts_nested_top_level_folder():
    svc = FakeSkillService()
    zip_bytes = _make_zip({"detect-rdp/SKILL.md": VALID_SKILL_MD})

    result = import_skill_zip(zip_bytes, service=svc)

    assert result["replaced"] is False
    assert result["name"] == "Detect Lateral RDP"


@pytest.mark.unit
def test_replace_bumps_version_when_name_collides():
    svc = FakeSkillService()
    zip_bytes_v1 = _make_zip({"SKILL.md": VALID_SKILL_MD})
    first = import_skill_zip(zip_bytes_v1, service=svc)
    assert first["version"] == 1

    updated_md = VALID_SKILL_MD.replace(
        b"Finds unusual RDP sessions.",
        b"Finds unusual RDP sessions on domain controllers.",
    )
    zip_bytes_v2 = _make_zip({"SKILL.md": updated_md})
    second = import_skill_zip(zip_bytes_v2, service=svc)

    assert second["replaced"] is True
    assert second["skill_id"] == first["skill_id"]
    assert second["version"] == 2


# --------------------------------------------------------------------------- #
# Validation failures
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_missing_skill_md_is_400():
    svc = FakeSkillService()
    zip_bytes = _make_zip({"README.md": b"not a skill"})

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400
    assert "SKILL.md" in excinfo.value.message


@pytest.mark.unit
def test_extra_files_alongside_skill_md_are_rejected():
    svc = FakeSkillService()
    zip_bytes = _make_zip(
        {
            "SKILL.md": VALID_SKILL_MD,
            "scripts/helper.py": b"print('hi')",
            "notes.txt": b"hello",
        }
    )

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400
    rejected = excinfo.value.details.get("rejected_paths") or []
    assert "scripts/helper.py" in rejected
    assert "notes.txt" in rejected


@pytest.mark.unit
def test_not_a_zip_is_400():
    svc = FakeSkillService()
    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(b"totally not a zip file", service=svc)
    assert excinfo.value.status_code == 400


@pytest.mark.unit
def test_missing_frontmatter_is_400():
    svc = FakeSkillService()
    zip_bytes = _make_zip({"SKILL.md": b"# Just markdown, no frontmatter"})

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400
    assert "frontmatter" in excinfo.value.message.lower()


@pytest.mark.unit
def test_missing_required_field_is_400():
    svc = FakeSkillService()
    # Missing 'category'
    md = b"""---
name: Has No Category
---
Body here."""
    zip_bytes = _make_zip({"SKILL.md": md})

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400
    assert "category" in excinfo.value.details.get("missing_fields", [])


@pytest.mark.unit
def test_invalid_category_is_400():
    svc = FakeSkillService()
    md = b"""---
name: Bad Category
category: banana
---
Body here."""
    zip_bytes = _make_zip({"SKILL.md": md})

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400
    assert "banana" in excinfo.value.message


@pytest.mark.unit
def test_empty_body_is_400():
    svc = FakeSkillService()
    md = b"""---
name: No Body
category: detection
---
"""
    zip_bytes = _make_zip({"SKILL.md": md})

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400
    assert "body" in excinfo.value.message.lower()


@pytest.mark.unit
def test_required_tools_scalar_string_coerced_to_list():
    svc = FakeSkillService()
    md = b"""---
name: Scalar Tool
category: enrichment
required_tools: virustotal.hash
---
Enrich it."""
    zip_bytes = _make_zip({"SKILL.md": md})

    result = import_skill_zip(zip_bytes, service=svc)
    row = svc._store[result["skill_id"]]
    assert row["required_tools"] == ["virustotal.hash"]


@pytest.mark.unit
def test_required_tools_wrong_type_is_400():
    svc = FakeSkillService()
    md = b"""---
name: Bad Tools
category: enrichment
required_tools:
  nested: wrong
---
Body."""
    zip_bytes = _make_zip({"SKILL.md": md})

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400


@pytest.mark.unit
def test_oversize_zip_is_413():
    svc = FakeSkillService()
    oversized = b"x" * (MAX_ZIP_BYTES + 1)

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(oversized, service=svc)
    assert excinfo.value.status_code == 413


@pytest.mark.unit
def test_too_many_entries_is_400():
    svc = FakeSkillService()
    entries = {"SKILL.md": VALID_SKILL_MD}
    for i in range(MAX_ENTRIES):
        entries[f"extra_{i}.txt"] = b"x"
    zip_bytes = _make_zip(entries)

    with pytest.raises(SkillImportError) as excinfo:
        import_skill_zip(zip_bytes, service=svc)
    assert excinfo.value.status_code == 400
    # Message differentiates between entry-count and extra-file rejection
    assert "entries" in excinfo.value.message.lower()


@pytest.mark.unit
def test_router_path_uses_module_level_service(monkeypatch):
    """The importer looks up SkillService at call time so router-level
    patches propagate. Regression guard: if someone replaces the import
    with `from services.skill_service import SkillService`, this breaks.
    """
    import services.skill_importer as importer_mod

    captured: Dict[str, Any] = {}

    class SpyService(FakeSkillService):
        def create_skill(self, data, created_by=None):
            captured["data"] = data
            captured["created_by"] = created_by
            return super().create_skill(data, created_by=created_by)

    monkeypatch.setattr(importer_mod._skill_service_mod, "SkillService", SpyService)

    zip_bytes = _make_zip({"SKILL.md": VALID_SKILL_MD})
    # No `service=` argument: importer must resolve SkillService at call time.
    result = import_skill_zip(zip_bytes, created_by="bob")

    assert result["replaced"] is False
    assert captured["created_by"] == "bob"
    assert captured["data"]["name"] == "Detect Lateral RDP"


# Silence unused-import warnings for constants used in assertions above.
_ = MAX_SKILL_MD_BYTES
_ = patch
