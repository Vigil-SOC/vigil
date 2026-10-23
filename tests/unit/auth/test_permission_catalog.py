"""The permission catalog and the revision that copies it onto roles (#2915)."""

import importlib.util
import re
from pathlib import Path

from core.auth.permissions import CATALOG
from core.upgrade.adopt import LEGACY_ROLE_IDS
from core.upgrade.command import PACKAGE_DIR
from core.upgrade.revision import note_stamped, stash_report, take_reports

_PATH = PACKAGE_DIR / "versions" / "0008_permission_catalog.py"
_SPEC = importlib.util.spec_from_file_location("permission_catalog_0008", _PATH)
revision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(revision)

_ORIGIN = re.compile(r"^split from \S+$|^signed-in read$|^new capability$")
_SQL = Path(revision.__file__).with_suffix(".sql").read_text(encoding="utf-8")


def test_every_permission_has_an_origin():
    permissions = CATALOG["permissions"]
    assert permissions, "the catalog names no permissions"
    for name, origin in permissions.items():
        assert isinstance(origin, str) and _ORIGIN.match(origin), (name, origin)


def test_revision_successors_match_catalog_origins():
    expected: dict[str, set[str]] = {}
    for name, origin in CATALOG["permissions"].items():
        if origin.startswith("split from "):
            expected.setdefault(origin.removeprefix("split from "), set()).add(name)
    assert {
        parent: set(keys) for parent, keys in revision.SUCCESSORS.items()
    } == expected


def test_revision_builtins_match_the_catalog():
    assert revision.BUILTINS == CATALOG["builtins"]


def test_revision_follows_the_conversation_generation():
    # One parent, the head on main. This file does not merge heads.
    assert revision.revision == "0008_permission_catalog"
    assert revision.down_revision == "0007_conversation_generation"
    assert isinstance(revision.down_revision, str)


def test_sql_grants_the_successor_map_and_keeps_settings():
    for keys in revision.SUCCESSORS.values():
        for key in keys:
            assert f'"{key}": true' in _SQL
    for key in (
        "detections.read",
        "automation.read",
        "ai_autonomy.read",
        "platform.read",
        "ai_decisions.read",
        "integrations.read",
    ):
        assert f'"{key}": true' in _SQL
    assert "role-senior-analyst" in _SQL and '"ai_autonomy.stop": true' in _SQL
    assert "role-admin" in _SQL and '"roles.manage": true' in _SQL
    # #2916 deletes these, in the change that stops checking them.
    assert "status = 'pending'" in _SQL
    assert "requires_approval = false" in _SQL
    assert "approved_by IS NULL" in _SQL


def test_a_report_stashed_before_the_version_row_updates_is_dropped():
    take_reports()
    stash_report("0008_permission_catalog", {"pending_returned": 1})
    assert take_reports() == []
    stash_report("0008_permission_catalog", {"pending_returned": 1})
    note_stamped("0008_permission_catalog")
    assert take_reports() == [
        {"revision": "0008_permission_catalog", "report": {"pending_returned": 1}}
    ]


def test_report_names_the_widening_the_diff_and_a_kept_legacy_role():
    viewer = dict(revision.BUILTINS["role-viewer"])
    viewer["settings.read"] = False
    viewer["integrations.read"] = False
    for key in (
        "detections.read",
        "automation.read",
        "ai_autonomy.read",
        "platform.read",
        "ai_decisions.read",
    ):
        viewer.pop(key)
    senior = dict(revision.BUILTINS["role-senior-analyst"])
    senior.pop("ai_autonomy.stop")
    senior.pop("integrations.install")
    senior.pop("ai_decisions.read")
    manager = dict(revision.BUILTINS["role-manager"])
    manager["findings.delete"] = False
    before = {
        "role-viewer": {"permissions": viewer, "is_system_role": True},
        "role-senior-analyst": {"permissions": senior, "is_system_role": True},
        "role-manager": {"permissions": manager, "is_system_role": True},
        "analyst": {
            "permissions": {"cases.write": True, "settings.write": False},
            "is_system_role": False,
        },
    }
    after = {
        "role-viewer": {
            "permissions": revision.BUILTINS["role-viewer"],
            "is_system_role": True,
        },
        "role-senior-analyst": {
            "permissions": revision.BUILTINS["role-senior-analyst"],
            "is_system_role": True,
        },
        "role-manager": {"permissions": manager, "is_system_role": True},
        "analyst": {
            "permissions": {
                "cases.write": True,
                "cases.assign": True,
                "settings.write": False,
                "detections.read": True,
            },
            "is_system_role": False,
        },
    }
    # Manager's findings.delete edit is not a copy, so it is still false after.
    after["role-manager"]["permissions"] = {
        **revision.BUILTINS["role-manager"],
        "findings.delete": False,
    }
    body = revision.report_for(before, after, 2)
    assert body["widened"] == {"role-senior-analyst": ["ai_autonomy.stop"]}
    assert "ai_autonomy.stop" in body["gained"]["role-senior-analyst"]
    assert "settings.read" in body["gained"]["role-viewer"]
    assert "integrations.read" in body["gained"]["role-viewer"]
    assert body["differs"] == [
        {
            "role_id": "role-manager",
            "diff": {"findings.delete": {"catalog": True, "role": False}},
        }
    ]
    assert body["legacy_roles"] == ["analyst"]
    assert set(body["legacy_roles"]).issubset(LEGACY_ROLE_IDS)
    assert body["pending_returned"] == 2
    assert "roles.manage" not in body["gained"].get("analyst", [])
