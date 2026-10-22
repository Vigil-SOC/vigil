"""What Vigil owns in the public schema, read from pg_catalog, and how two differ.

``schema_state`` flattens tables, columns, defaults, indexes, constraints,
triggers (and whether they are enabled), views, functions, sequences, row
security and policies, owners and grants (column grants and default
privileges included) into ``{object: description}``.
Role names are mapped by role, not literal name: the connecting user is
``owner`` and ``vigil_app`` is ``app``, so databases owned by different
logins compare equal. Objects that belong to an extension and Alembic's own
``alembic_version`` are left out.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

APP_ROLE = "vigil_app"
VERSION_TABLE = "alembic_version"

# Members of extensions (pg_trgm puts its functions in public).
_NOT_EXTENSION = """
    NOT EXISTS (SELECT 1 FROM pg_depend d
                WHERE d.classid = {catalog}::regclass AND d.objid = {oid}
                  AND d.deptype = 'e')
"""


def _acl(conn: Connection, acl_sql: str, roles: dict[str, str]) -> list[str]:
    """``grantee:PRIVILEGE`` (``*`` when grantable), grantee mapped by role."""
    rows = conn.execute(text(f"""
        SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
               a.privilege_type, a.is_grantable
        FROM aclexplode({acl_sql}) a
    """))
    return sorted(
        f"{_role(g, roles)}:{p}{'*' if grantable else ''}" for g, p, grantable in rows
    )


def _role(name: str, roles: dict[str, str]) -> str:
    return roles.get(name, name)


def schema_state(conn: Connection) -> dict[str, Any]:
    owner = conn.execute(text("SELECT current_user")).scalar_one()
    roles = {owner: "owner", APP_ROLE: "app"}
    state: dict[str, Any] = {}

    for (name,) in conn.execute(text("SELECT extname FROM pg_extension")):
        if name != "plpgsql":
            state[f"extension {name}"] = True

    nsp_owner = conn.execute(
        text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'"
        )
    ).scalar_one()
    state["schema public"] = {
        "owner": _role(nsp_owner, roles),
        "acl": _acl(
            conn,
            "(SELECT coalesce(nspacl, acldefault('n', nspowner)) "
            "FROM pg_namespace WHERE nspname = 'public')",
            roles,
        ),
    }

    relations = conn.execute(
        text(f"""
        SELECT c.oid, c.relname, c.relkind, pg_get_userbyid(c.relowner),
               c.relrowsecurity, c.relforcerowsecurity,
               CASE WHEN s.seqrelid IS NOT NULL THEN json_build_array(
                   format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement,
                   s.seqmin, s.seqmax, s.seqcache, s.seqcycle)::text END
        FROM pg_class c
        LEFT JOIN pg_sequence s ON s.seqrelid = c.oid
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND c.relname <> :version_table
          AND {_NOT_EXTENSION.format(catalog="'pg_class'", oid="c.oid")}
        ORDER BY c.relname
    """),
        {"version_table": VERSION_TABLE},
    ).all()
    for oid, name, kind, rel_owner, rls, force_rls, sequence in relations:
        state[f"relation {name}"] = {
            "kind": kind,
            "row_security": [rls, force_rls],
            "sequence": sequence,
            "owner": _role(rel_owner, roles),
            "acl": _acl(
                conn,
                "(SELECT coalesce(relacl, acldefault("
                "(CASE WHEN relkind = 'S' THEN 's' ELSE 'r' END)::\"char\", relowner)) "
                f"FROM pg_class WHERE oid = {int(oid)})",
                roles,
            ),
        }

    for (
        table,
        column,
        data_type,
        not_null,
        default,
        attrelid,
        attnum,
        acl,
    ) in conn.execute(
        text("""
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
               a.attnotnull, pg_get_expr(d.adbin, d.adrelid),
               a.attrelid, a.attnum, a.attacl IS NOT NULL
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND c.relname <> :version_table
          AND a.attnum > 0 AND NOT a.attisdropped
    """),
        {"version_table": VERSION_TABLE},
    ):
        entry = {"type": data_type, "nullable": not not_null, "default": default}
        if acl:  # column-level grants
            entry["acl"] = _acl(
                conn,
                "(SELECT attacl FROM pg_attribute "
                f"WHERE attrelid = {int(attrelid)} AND attnum = {int(attnum)})",
                roles,
            )
        state[f"column {table}.{column}"] = entry

    queries = {
        "index": """
            SELECT i.relname, pg_get_indexdef(i.oid)
            FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_class t ON t.oid = x.indrelid
            WHERE i.relnamespace = 'public'::regnamespace
              AND t.relname <> :version_table
        """,
        "constraint": """
            SELECT t.relname || '.' || k.conname, pg_get_constraintdef(k.oid)
            FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid
            WHERE t.relnamespace = 'public'::regnamespace
              AND t.relname <> :version_table
        """,
        "trigger": """
            SELECT t.relname || '.' || g.tgname, pg_get_triggerdef(g.oid)
                   || CASE WHEN g.tgenabled = 'O' THEN ''
                      ELSE ' [enabled=' || g.tgenabled::text || ']' END
            FROM pg_trigger g JOIN pg_class t ON t.oid = g.tgrelid
            WHERE t.relnamespace = 'public'::regnamespace AND NOT g.tgisinternal
        """,
        "policy": """
            SELECT tablename || '.' || policyname,
                   concat_ws(' ', permissive, cmd, roles::text, qual, with_check)
            FROM pg_policies WHERE schemaname = 'public'
        """,
        "view": """
            SELECT c.relname, pg_get_viewdef(c.oid)
            FROM pg_class c
            WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('v', 'm')
        """,
    }
    for kind, sql in queries.items():
        for name, definition in conn.execute(
            text(sql), {"version_table": VERSION_TABLE}
        ):
            state[f"{kind} {name}"] = definition

    for oid, signature, fn_owner, definition in conn.execute(text(f"""
        SELECT p.oid, p.oid::regprocedure::text, pg_get_userbyid(p.proowner),
               pg_get_functiondef(p.oid)
        FROM pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace AND p.prokind IN ('f', 'p')
          AND {_NOT_EXTENSION.format(catalog="'pg_proc'", oid="p.oid")}
    """)):
        state[f"function {signature}"] = {
            "owner": _role(fn_owner, roles),
            "acl": _acl(
                conn,
                "(SELECT coalesce(proacl, acldefault('f', proowner)) "
                f"FROM pg_proc WHERE oid = {int(oid)})",
                roles,
            ),
            "definition": definition,
        }

    for oid, role, objtype in conn.execute(text("""
        SELECT oid, pg_get_userbyid(defaclrole), defaclobjtype::text
        FROM pg_default_acl
        WHERE defaclnamespace IN (0, 'public'::regnamespace)
    """)):
        state[f"default_acl {_role(role, roles)}.{objtype}"] = _acl(
            conn,
            f"(SELECT defaclacl FROM pg_default_acl WHERE oid = {int(oid)})",
            roles,
        )

    return dict(sorted(state.items()))


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    """Every object that is missing, extra or different, with both sides."""
    return [
        {"object": key, "expected": expected.get(key), "actual": actual.get(key)}
        for key in sorted(expected.keys() | actual.keys())
        if expected.get(key) != actual.get(key)
    ]
