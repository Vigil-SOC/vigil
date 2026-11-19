"""Unwrap a scalar indicator stored as one-element list text (#4848).

A lone ``src_ip``, ``dst_ip``, ``hostname`` or ``user`` was turned into a
one-item list, then stored as ``str`` of that list (``['198.51.100.20']``).
Rows of ``shared_iocs`` and ``case_iocs`` whose value is exactly that text
become the plain string. Anything else stays. The text is not re-keyed:
index values are already trimmed, defanged and lowercased.

``shared_iocs`` is unique on (case_id, ioc_type, value). When the plain row
is already there, the bracketed row is deleted; otherwise one row is kept and
rewritten. ``case_iocs`` has no such unique: when a plain row is already
there, the bracketed row is deleted and ``is_false_positive`` stays set on
the plain row if either row was marked. A marked address may remain in the
index as the plain value until the next reconcile; matching drops it on read.
"""

from __future__ import annotations

import ast
from collections import defaultdict

from alembic import op
from sqlalchemy import bindparam, text

from core.upgrade.revision import stash_report

revision = "0102_unwrap_scalar_ioc_text"
down_revision = "0101_sso_required_end_reason"


def _one_string_list(value: object) -> str | None:
    """The single string inside ``str([s])``, or None when ``value`` is not that text."""
    if (
        not isinstance(value, str)
        or len(value) < 4
        or value[0] != "["
        or value[-1] != "]"
    ):
        return None
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError, MemoryError, RecursionError, TypeError):
        return None
    if (
        isinstance(parsed, list)
        and len(parsed) == 1
        and isinstance(parsed[0], str)
        and str(parsed) == value
    ):
        return parsed[0]
    return None


def _rewrite(conn, table: str, pk: str, *, collapse: bool, marks: bool) -> dict:
    """Unwrap one-element list text in ``table``. Counts are rewritten, removed, marks set."""
    marked = ", is_false_positive AS marked" if marks else ", false AS marked"
    candidates = conn.execute(text(f"""
            SELECT {pk} AS id, case_id, ioc_type, value{marked}
            FROM public.{table}
            WHERE left(value, 1) = '[' AND right(value, 1) = ']'
            """)).all()
    plain_of = {
        row.id: plain
        for row in candidates
        if (plain := _one_string_list(row.value)) is not None
    }
    counts = {"rewritten": 0, "removed": 0, "marks_set": 0}
    if not plain_of:
        return counts

    case_ids = list({row.case_id for row in candidates if row.id in plain_of})
    held = conn.execute(
        text(f"""
            SELECT {pk} AS id, case_id, ioc_type, value{marked}
            FROM public.{table}
            WHERE case_id IN :ids
            """).bindparams(bindparam("ids", expanding=True)),
        {"ids": case_ids},
    ).all()

    at_value: dict[tuple, list] = defaultdict(list)
    for row in held:
        at_value[(row.case_id, row.ioc_type, row.value)].append(row)
    movers_of: dict[tuple, list] = defaultdict(list)
    for row in held:
        if row.id in plain_of:
            movers_of[(row.case_id, row.ioc_type, plain_of[row.id])].append(row)

    delete_ids: list[int] = []
    updates: list[tuple[int, str]] = []
    flag_ids: list[int] = []
    for key, movers in movers_of.items():
        stables = [row for row in at_value.get(key, []) if row.id not in plain_of]
        if stables:
            if any(row.marked for row in movers):
                flag_ids.extend(row.id for row in stables if not row.marked)
            delete_ids.extend(row.id for row in movers)
            continue
        if collapse:
            keep = min(movers, key=lambda row: row.id)
            delete_ids.extend(row.id for row in movers if row.id != keep.id)
            updates.append((keep.id, key[2]))
        else:
            updates.extend((row.id, key[2]) for row in movers)

    if flag_ids:
        conn.execute(
            text(f"""
                UPDATE public.{table}
                SET is_false_positive = true
                WHERE {pk} IN :ids
                """).bindparams(bindparam("ids", expanding=True)),
            {"ids": flag_ids},
        )
    if delete_ids:
        conn.execute(
            text(f"DELETE FROM public.{table} WHERE {pk} IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": delete_ids},
        )
    if updates:
        # A bracketed value can be another row's plain target. Park each
        # keeper on a value nothing holds, then write the plain text.
        taken = {row.value for row in held}

        def placeholder(row_id: int) -> str:
            token = f"\x01{row_id}\x01"
            while token in taken:
                token += "\x01"
            taken.add(token)
            return token

        conn.execute(
            text(f"UPDATE public.{table} SET value = :token WHERE {pk} = :id"),
            [{"id": row_id, "token": placeholder(row_id)} for row_id, _ in updates],
        )
        conn.execute(
            text(f"UPDATE public.{table} SET value = :value WHERE {pk} = :id"),
            [{"id": row_id, "value": plain} for row_id, plain in updates],
        )

    counts["rewritten"] = len(updates)
    counts["removed"] = len(delete_ids)
    counts["marks_set"] = len(flag_ids)
    return counts


def upgrade() -> None:
    conn = op.get_bind()
    report = {}
    for table, pk, collapse, marks, prefix in (
        ("shared_iocs", "id", True, False, "shared_iocs"),
        ("case_iocs", "ioc_id", False, True, "case_iocs"),
    ):
        present = conn.execute(
            text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
        ).scalar()
        counts = (
            _rewrite(conn, table, pk, collapse=collapse, marks=marks)
            if present is not None
            else {"rewritten": 0, "removed": 0, "marks_set": 0}
        )
        report[f"{prefix}_rewritten"] = counts["rewritten"]
        report[f"{prefix}_removed"] = counts["removed"]
        if marks:
            report[f"{prefix}_marks_set"] = counts["marks_set"]
    stash_report(revision, report)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
