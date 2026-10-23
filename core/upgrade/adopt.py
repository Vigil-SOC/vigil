"""Adopt a 0.7.0 or 0.8.0 install: repair known drift, then stamp the baseline.

An existing database never runs ``0001_baseline.sql``. One transaction, on the
owner's connection, runs only these repairs, in this order:

1. objects in ``public`` owned by ``vigil_app`` move to the connecting owner,
   before any ALTER or CREATE INDEX needs it;
2. the init files Helm skipped (``HELM_SKIPS``), and ``34_mcp_credentials.sql``
   when that table is absent;
3. grants to ``deeptempo`` left by the 0.7 copies of the init files are revoked
   (the role is kept), and the legacy roles admin/analyst/viewer that no
   ``users.role_id`` holds are deleted; a held one is kept and named;
4. the forward suffix for the stamp (``FORWARD``): the init files added after
   that release, plus the two findings columns only create_all ever built;
5. ``30_vigil_app_role.sql``, so its grants cover what the suffix created.

The result is compared with ``head_schema.json``. Any remaining difference in an
object Vigil owns is unknown drift: the caller rolls back and refuses, with no
override. Objects Vigil does not own are listed and do not block.
``system_config.schema_version`` is read, never written or deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from core.storage.reference_seed import find_seed_file
from core.upgrade.schema import APP_ROLE, compare, schema_state

BASELINE = "0001_baseline"
LEGACY_OWNER = "deeptempo"
LEGACY_ROLE_IDS = ("admin", "analyst", "viewer")

HELM_SKIPS = (
    "05_case_management_extended.sql",
    "28_case_closure_actor.sql",
    "39_case_findings_finding_index.sql",
    "34_drop_skills.sql",
)
MCP_CREDENTIALS = "34_mcp_credentials.sql"
# Helm never re-ran 06, so a Helm install can lack this column.
MFA_STEP = "06_auth_tables.sql: mfa_last_totp_step"
# 0.8.0 got these from create_all and no init file has them; before this
# module, scripts/migrate_schema.py (add_findings_noise_reason) added them.
NOISE_STEP = "findings noise_marked_by_kind, noise_reason"
INLINE = {
    NOISE_STEP: "ALTER TABLE findings"
    " ADD COLUMN IF NOT EXISTS noise_marked_by_kind VARCHAR(20),"
    " ADD COLUMN IF NOT EXISTS noise_reason TEXT"
}
FORWARD = (
    "40_drop_custom_workflow_graph_layout.sql",
    "40_skill_reads.sql",
    "41_workflow_runs_progress.sql",
    "42_drop_finding_status.sql",
    "43_users_service_account.sql",
    MFA_STEP,
    NOISE_STEP,
    "44_medic_last_seen.sql",
    "45_medic_permissions.sql",
    "46_users_tokens_valid_after.sql",
)
# The stamps adoption accepts, and the suffix each one runs. A 0.8.0 stamp
# can be a 0.7 install upgraded in place, which never got the two columns
# only 06 and create_all build; both steps are no-ops where they exist.
# 0.8.0 already applied everything before the mfa step.
_FROM_0_8 = FORWARD[-5:]
FORWARD_BY_STAMP = {"0.7.0": FORWARD, "0.8.0": _FROM_0_8}
# Waits for a lock longer than this fail the run instead of queueing the app
# behind an ALTER.
LOCK_TIMEOUT = "30s"
APP_ROLE_FILE = "30_vigil_app_role.sql"

# Every release up to 0.8 ran the init path that writes schema_version.
LAST_PRE_BASELINE = (0, 8)


class AdoptionError(RuntimeError):
    """A repair file could not be found."""


@dataclass
class Adoption:
    stamp: str
    repairs: list[str] = field(default_factory=list)
    removed_roles: list[str] = field(default_factory=list)
    # Legacy role id -> users holding it.
    kept_roles: dict[str, int] = field(default_factory=dict)
    # Differences that remain in objects Vigil owns: these refuse.
    drift: list[dict[str, Any]] = field(default_factory=list)
    # Objects Vigil does not own: listed, never blocking.
    unowned: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "from": self.stamp,
            "repairs": self.repairs,
            "removed_roles": self.removed_roles,
            "kept_roles": self.kept_roles,
            "unowned": self.unowned,
        }


def read_stamp(conn: Connection) -> Optional[str]:
    """``system_config.schema_version``, or None when there is none."""
    if (
        conn.execute(text("SELECT to_regclass('public.system_config')")).scalar()
        is None
    ):
        return None
    return conn.execute(
        text(
            "SELECT value ->> 'version' FROM system_config WHERE key = 'schema_version'"
        )
    ).scalar()


def _pre_baseline(version: Optional[str]) -> bool:
    match = re.match(r"(\d+)\.(\d+)\.", version or "")
    if match is None:
        return False
    return (int(match[1]), int(match[2])) <= LAST_PRE_BASELINE


def restamped(conn: Connection) -> Optional[str]:
    """The pre-baseline version an older release stamped after the baseline, or None.

    Adoption leaves the old stamp as it found it, and a post-baseline
    ``init_database()`` no longer writes it, so only a ``schema_version`` row
    written in a later transaction than ``alembic_version``'s counts. A restore
    loads both in one transaction.
    """
    if (
        conn.execute(text("SELECT to_regclass('public.system_config')")).scalar()
        is None
    ):
        return None
    version = conn.execute(text("""
        SELECT s.value ->> 'version'
        FROM system_config s, alembic_version a
        WHERE s.key = 'schema_version'
          AND s.xmin <> a.xmin AND age(s.xmin) < age(a.xmin)
    """)).scalar()
    return version if _pre_baseline(version) else None


def _sql(name: str) -> str:
    if name in INLINE:
        return INLINE[name]
    filename = name.split(":")[0]
    path = find_seed_file(filename=filename)
    if path is None:
        raise AdoptionError(f"{filename} not found under database/init")
    sql = path.read_text(encoding="utf-8")
    if name == MFA_STEP:
        match = re.search(
            r"^ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_last_totp_step\b.*;$",
            sql,
            re.M,
        )
        if match is None:
            raise AdoptionError(f"no mfa_last_totp_step ALTER in {filename}")
        return match.group(0)
    return sql


def _run(conn: Connection, adoption: Adoption, name: str) -> None:
    # The raw cursor, as core/upgrade/revision.py does: '%' and ':name' stay literal.
    cursor = conn.connection.cursor()
    try:
        cursor.execute(_sql(name))
        cursor.execute("RESET ALL")
        _lock_timeout(cursor)
    finally:
        cursor.close()
    adoption.repairs.append(name)


def _lock_timeout(cursor) -> None:
    cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")


def _role_exists(conn: Connection, role: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
    )


def _reassign_app_objects(conn: Connection, adoption: Adoption) -> None:
    if not _role_exists(conn, APP_ROLE):
        return
    # Sequences a column owns move with their table and refuse ALTER on their own.
    rows = conn.execute(text("""
        SELECT c.relkind, format('%I.%I', n.nspname, c.relname)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relowner = CAST(:app AS regrole)
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
          AND NOT (c.relkind = 'S' AND EXISTS (
              SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid AND d.deptype IN ('a', 'i')))
        ORDER BY c.relkind = 'S', c.relname
    """).bindparams(app=APP_ROLE)).all()
    kinds = {
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "S": "SEQUENCE",
        "f": "FOREIGN TABLE",
    }
    functions = conn.execute(text("""
        SELECT p.oid::regprocedure::text FROM pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace AND p.proowner = CAST(:app AS regrole)
    """).bindparams(app=APP_ROLE)).scalars().all()
    if not rows and not functions:
        return
    # ALTER OWNER needs the old owner's privileges. A non-superuser owner
    # (CREATEROLE on an external Postgres) holds it only for this transaction.
    member = conn.execute(
        text("SELECT pg_has_role(:app, 'USAGE')").bindparams(app=APP_ROLE)
    ).scalar()
    if not member:
        conn.execute(text(f"GRANT {APP_ROLE} TO CURRENT_USER"))
    for kind, name in rows:
        conn.execute(
            text(f"ALTER {kinds.get(kind, 'TABLE')} {name} OWNER TO CURRENT_USER")
        )
    for signature in functions:
        conn.execute(text(f"ALTER ROUTINE {signature} OWNER TO CURRENT_USER"))
    if not member:
        conn.execute(text(f"REVOKE {APP_ROLE} FROM CURRENT_USER"))
    adoption.repairs.append(
        f"owner of {len(rows) + len(functions)} {APP_ROLE} object(s)"
    )


def _revoke_legacy_owner(conn: Connection, adoption: Adoption) -> None:
    current = conn.execute(text("SELECT current_user")).scalar_one()
    if current == LEGACY_OWNER or not _role_exists(conn, LEGACY_OWNER):
        return
    # Only on what this owner owns: a REVOKE on someone else's table errors.
    granted = conn.execute(text("""
        SELECT c.relkind = 'S', format('%I.%I', 'public', c.relname)
        FROM pg_class c
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relowner = CAST(current_user AS regrole)
          AND EXISTS (SELECT 1 FROM aclexplode(c.relacl) a
                      WHERE a.grantee = CAST(:legacy AS regrole))
    """).bindparams(legacy=LEGACY_OWNER)).all()
    for sequence, name in granted:
        kind = "SEQUENCE" if sequence else "TABLE"
        conn.execute(text(f"REVOKE ALL ON {kind} {name} FROM {LEGACY_OWNER}"))
    if granted:
        adoption.repairs.append(f"grants to {LEGACY_OWNER} revoked")


def _remove_legacy_roles(conn: Connection, adoption: Adoption) -> None:
    held = conn.execute(
        text(
            "SELECT role_id, count(*) FROM users WHERE role_id IN :ids GROUP BY role_id"
        ).bindparams(bindparam("ids", value=list(LEGACY_ROLE_IDS), expanding=True))
    ).all()
    adoption.kept_roles = {role: int(n) for role, n in sorted(held)}
    removable = [r for r in LEGACY_ROLE_IDS if r not in adoption.kept_roles]
    if removable:
        adoption.removed_roles = sorted(
            conn.execute(
                text(
                    "DELETE FROM roles WHERE role_id IN :ids RETURNING role_id"
                ).bindparams(bindparam("ids", value=removable, expanding=True))
            )
            .scalars()
            .all()
        )


def _index_table(definition: str) -> Optional[str]:
    match = re.search(
        r" ON (?:ONLY )?(?:public\.)?\"?([^\s\"(]+)\"? USING ", definition
    )
    return match.group(1) if match else None


def _owned(entry: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Whether a difference touches something Vigil owns: an object the head
    schema has, or anything on one of its tables."""
    key = entry["object"]
    if key in expected:
        return True
    kind, _, name = key.partition(" ")
    if kind in ("column", "constraint", "trigger", "policy"):
        table = name.split(".", 1)[0]
    elif kind == "index":
        table = _index_table(str(entry["actual"]))
    else:
        return False
    return f"relation {table}" in expected


def adopt(conn: Connection, stamp: str, expected: dict[str, Any]) -> Adoption:
    """Run the repairs for ``stamp`` and compare; the caller owns the transaction."""
    adoption = Adoption(stamp)
    conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    _reassign_app_objects(conn, adoption)
    for name in HELM_SKIPS:
        _run(conn, adoption, name)
    if (
        conn.execute(text("SELECT to_regclass('public.mcp_credentials')")).scalar()
        is None
    ):
        _run(conn, adoption, MCP_CREDENTIALS)
    _revoke_legacy_owner(conn, adoption)
    _remove_legacy_roles(conn, adoption)
    for name in FORWARD_BY_STAMP[stamp]:
        _run(conn, adoption, name)
    _run(conn, adoption, APP_ROLE_FILE)

    for entry in compare(expected, schema_state(conn)):
        (adoption.drift if _owned(entry, expected) else adoption.unowned).append(entry)
    return adoption


def stamp_baseline(conn: Connection, script) -> None:
    """Record the baseline in the adoption's transaction; only the owner moves it."""
    from alembic.runtime.migration import MigrationContext

    MigrationContext.configure(conn, opts={"version_table_schema": "public"}).stamp(
        script, BASELINE
    )
    conn.execute(text(f"REVOKE ALL ON TABLE public.alembic_version FROM {APP_ROLE}"))
