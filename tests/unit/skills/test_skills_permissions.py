"""Saving, uploading and deleting a skill needs ``automation.write``.

A skill's description reaches every agent's system prompt, so a role that may
not change automation may not write one. The grants are the built-ins as the
catalog revision leaves them, so a change that hands an analyst role
``automation.write`` fails here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.auth.auth_service import AuthService
from core.auth.permissions import CATALOG
from core.config import Settings
from tests.unit.skills.conftest import skills_app

pytestmark = pytest.mark.unit

EXPECTED = {
    "role-viewer": 403,
    "role-analyst": 403,
    "role-senior-analyst": 403,
    "role-manager": 200,
    "role-admin": 200,
}
GRANTS = CATALOG["builtins"]


def test_every_seeded_role_is_expected():
    assert set(GRANTS) == set(EXPECTED)


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_only_a_role_that_may_change_automation_writes_a_skill(
    role, tmp_path, monkeypatch
):
    root = tmp_path / "operator"
    root.mkdir()
    settings = Settings(vigil_skills_path=str(root))
    monkeypatch.setattr("core.skills.skill_library.get_settings", lambda: settings)
    monkeypatch.setattr(
        AuthService,
        "check_permission",
        lambda _user_id, permission, session=None: GRANTS[role].get(permission) is True,
    )
    client = TestClient(skills_app())

    saved = client.post(
        "/api/skills",
        json={"name": "desk-check", "description": "d", "body": "# Steps\n"},
    )
    uploaded = client.post(
        "/api/skills/upload",
        files={
            "file": ("SKILL.md", b"---\nname: night-shift\ndescription: d\n---\n\nX\n")
        },
    )
    deleted = client.delete("/api/skills/desk-check")

    statuses = [saved.status_code, uploaded.status_code, deleted.status_code]
    assert statuses == [EXPECTED[role]] * 3
    written = ["night-shift"] if EXPECTED[role] == 200 else []
    assert sorted(p.name for p in root.iterdir()) == written
