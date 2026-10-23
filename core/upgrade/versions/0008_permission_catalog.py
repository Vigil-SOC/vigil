"""Copy successor permissions and record what each role gained (#2915).

The grants are literals in the SQL beside this file. They are not read from
``core.auth.permissions``: a later catalog edit must not change what this
revision writes on a fresh install. ``BUILTINS`` is that catalog's built-in
definitions, frozen here for the same reason.

``settings.read`` and ``settings.write`` stay. #2916 deletes them when it
moves the last gate off them.
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

from core.upgrade.adopt import LEGACY_ROLE_IDS
from core.upgrade.revision import run_sql, stash_report

revision = "0008_permission_catalog"
down_revision = "0007_conversation_generation"

# Parent key -> successors copied only when the parent is true.
SUCCESSORS = {
    "settings.write": (
        "detections.write",
        "automation.write",
        "ai_autonomy.write",
        "ai_autonomy.stop",
        "platform.write",
    ),
    "settings.read": (
        "detections.read",
        "automation.read",
        "ai_autonomy.read",
        "platform.read",
    ),
    "integrations.write": ("integrations.install",),
    "cases.write": ("cases.assign",),
}

# Stop is also granted to Senior Analyst, who does not hold settings.write.
# The report lists that as a widening, not a copy.
WIDENING = {"role-senior-analyst": ("ai_autonomy.stop",)}
_WIDENING_PARENT = {"ai_autonomy.stop": "settings.write"}

# Frozen at this revision. tests/unit/auth/test_permission_catalog.py checks
# it against the live catalog.
BUILTINS = {
    "role-admin": {
        "ai_autonomy.read": True,
        "ai_autonomy.stop": True,
        "ai_autonomy.write": True,
        "ai_chat.use": True,
        "ai_decisions.approve": True,
        "ai_decisions.read": True,
        "audit.forward": True,
        "audit.manage": True,
        "audit.read": True,
        "automation.read": True,
        "automation.write": True,
        "cases.assign": True,
        "cases.delete": True,
        "cases.read": True,
        "cases.write": True,
        "detections.read": True,
        "detections.write": True,
        "findings.delete": True,
        "findings.read": True,
        "findings.write": True,
        "integrations.install": True,
        "integrations.read": True,
        "integrations.write": True,
        "loglm.view": True,
        "medic.admin": True,
        "medic.read": True,
        "platform.read": True,
        "platform.write": True,
        "roles.manage": True,
        "settings.read": True,
        "settings.write": True,
        "users.delete": True,
        "users.read": True,
        "users.write": True,
    },
    "role-analyst": {
        "ai_autonomy.read": True,
        "ai_chat.use": True,
        "ai_decisions.approve": False,
        "ai_decisions.read": True,
        "automation.read": True,
        "cases.assign": True,
        "cases.delete": False,
        "cases.read": True,
        "cases.write": True,
        "detections.read": True,
        "findings.delete": False,
        "findings.read": True,
        "findings.write": True,
        "integrations.read": True,
        "integrations.write": False,
        "loglm.view": True,
        "platform.read": True,
        "settings.read": True,
        "settings.write": False,
        "users.read": False,
    },
    "role-manager": {
        "ai_autonomy.read": True,
        "ai_autonomy.stop": True,
        "ai_autonomy.write": True,
        "ai_chat.use": True,
        "ai_decisions.approve": True,
        "ai_decisions.read": True,
        "audit.read": True,
        "automation.read": True,
        "automation.write": True,
        "cases.assign": True,
        "cases.delete": True,
        "cases.read": True,
        "cases.write": True,
        "detections.read": True,
        "detections.write": True,
        "findings.delete": True,
        "findings.read": True,
        "findings.write": True,
        "integrations.install": True,
        "integrations.read": True,
        "integrations.write": True,
        "loglm.view": True,
        "medic.read": True,
        "platform.read": True,
        "platform.write": True,
        "settings.read": True,
        "settings.write": True,
        "users.delete": False,
        "users.read": True,
        "users.write": True,
    },
    "role-senior-analyst": {
        "ai_autonomy.read": True,
        "ai_autonomy.stop": True,
        "ai_chat.use": True,
        "ai_decisions.approve": True,
        "ai_decisions.read": True,
        "automation.read": True,
        "cases.assign": True,
        "cases.delete": False,
        "cases.read": True,
        "cases.write": True,
        "detections.read": True,
        "findings.delete": True,
        "findings.read": True,
        "findings.write": True,
        "integrations.install": True,
        "integrations.read": True,
        "integrations.write": True,
        "loglm.view": True,
        "platform.read": True,
        "settings.read": True,
        "settings.write": False,
        "users.read": True,
    },
    "role-viewer": {
        "ai_autonomy.read": True,
        "ai_chat.use": False,
        "ai_decisions.approve": False,
        "ai_decisions.read": True,
        "automation.read": True,
        "cases.read": True,
        "detections.read": True,
        "findings.read": True,
        "integrations.read": True,
        "platform.read": True,
        "settings.read": True,
        "users.read": False,
    },
}


def _permissions(value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


def _load_roles(conn) -> dict[str, dict]:
    rows = conn.execute(
        text("SELECT role_id, permissions, is_system_role FROM public.roles")
    )
    return {
        role_id: {
            "permissions": _permissions(permissions),
            "is_system_role": bool(is_system),
        }
        for role_id, permissions, is_system in rows
    }


def _pending_count(conn) -> int:
    return int(conn.execute(text("""
                SELECT count(*) FROM public.approval_actions
                WHERE status = 'approved'
                  AND requires_approval = false
                  AND approved_by IS NULL
                """)).scalar_one())


def _diff(actual: dict, expected: dict) -> dict:
    diff = {}
    for key in sorted(set(actual) | set(expected)):
        role_value = actual[key] if key in actual else None
        catalog_value = expected[key] if key in expected else None
        if role_value != catalog_value:
            diff[key] = {"catalog": catalog_value, "role": role_value}
    return diff


def report_for(before: dict, after: dict, pending: int) -> dict:
    """Keys gained, built-ins that differ, kept legacy roles, rows returned."""
    gained = {}
    widened = {}
    for role_id in sorted(after):
        previous = before.get(role_id, {}).get("permissions", {})
        current = after[role_id]["permissions"]
        keys = sorted(
            key
            for key, value in current.items()
            if value is True and previous.get(key) is not True
        )
        if keys:
            gained[role_id] = keys
        extra = [
            key
            for key in WIDENING.get(role_id, ())
            if key in keys and previous.get(_WIDENING_PARENT[key]) is not True
        ]
        if extra:
            widened[role_id] = extra
    differs = []
    for role_id in sorted(BUILTINS):
        row = after.get(role_id)
        if row is None:
            continue
        diff = _diff(row["permissions"], BUILTINS[role_id])
        if diff:
            differs.append({"role_id": role_id, "diff": diff})
    legacy = sorted(role_id for role_id in after if role_id in LEGACY_ROLE_IDS)
    return {
        "gained": gained,
        "widened": widened,
        "differs": differs,
        "legacy_roles": legacy,
        "pending_returned": pending,
    }


def _trail(conn, body: dict) -> None:
    from core.audit import trail
    from core.version import __version__

    if not trail.trail_ready(conn):
        return
    trail.append(
        conn,
        {
            trail.ACTION: "roles.revised",
            trail.ACTOR: trail.SYSTEM,
            "release": __version__,
            "report": body,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    before = _load_roles(conn)
    pending = _pending_count(conn)
    run_sql(__file__)
    body = report_for(before, _load_roles(conn), pending)
    _trail(conn, body)
    stash_report(revision, body)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
