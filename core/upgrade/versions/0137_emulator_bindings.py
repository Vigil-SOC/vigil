"""Emulator marks become emulation bindings on class rows (#5315).

Each tool the stored ``emulation.tools`` marks, beyond the two Atomic Red Team
spellings, gets ``{"emulation": true, "server": null}`` on its
``tool_effect_classes`` row: the server is never split from the flattened name.
A tool with no row gets one at ``irreversible``, written by ``system``, which
gates it as being unclassified did. Atomic's emulator binding now ships in
``core/integrations/bindings.yaml``; a stored list that left it out unbinds it
(``{}``). A row whose binding is already set is left and logged. Then ``tools``
is dropped from the stored value, which keeps ``hosts``. Each write has its
trail entry. A second run finds no ``tools`` and changes nothing.
"""

from __future__ import annotations

import json
import logging

from alembic import op
from sqlalchemy import text

revision = "0137_emulator_bindings"
down_revision = "0136_falcon_host_search_read"

logger = logging.getLogger(__name__)

KEY = "emulation"
ATOMIC = "atomic-red-team_atomic_red_team_execute"
ATOMIC_TOOLS = (ATOMIC, "atomic_red_team_execute")
EMULATOR = {"emulation": True, "server": None}


def _trail(conn, payload: dict) -> None:
    from core.audit import trail

    if trail.trail_ready(conn):
        trail.append(conn, {trail.ACTOR: trail.SYSTEM, **payload})


def _shipped_capabilities() -> set:
    from core.integrations import bindings

    return {n for n, b in bindings.shipped().items() if b.capability != "emulation"}


def _bind(conn, name: str, binding: dict) -> None:
    row = conn.execute(
        text("SELECT binding FROM public.tool_effect_classes WHERE tool_name = :n"),
        {"n": name},
    ).first()
    if row is not None and row[0] is not None:
        logger.warning(
            "%s already has binding %r; its emulator mark is not carried over",
            name,
            row[0],
        )
        return
    if name in _shipped_capabilities():
        logger.warning(
            "%s was marked: its emulator binding replaces the shipped one", name
        )
    if row is None:
        conn.execute(
            text("""
                INSERT INTO public.tool_effect_classes
                    (tool_name, effect_class, classified_by, binding)
                VALUES (:n, 'irreversible', 'system', CAST(:b AS jsonb))
                """),
            {"n": name, "b": json.dumps(binding)},
        )
        _trail(
            conn,
            {
                "action": "autonomy.tool_classified",
                "tool": name,
                "class_before": "unclassified",
                "class_after": "irreversible",
            },
        )
    else:
        conn.execute(
            text("""
                UPDATE public.tool_effect_classes SET binding = CAST(:b AS jsonb)
                WHERE tool_name = :n
                """),
            {"n": name, "b": json.dumps(binding)},
        )
    _trail(
        conn,
        {
            "action": "autonomy.tool_bound",
            "tool": name,
            "binding_before": "shipped",
            "binding_after": binding,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table in ("system_config", "tool_effect_classes"):
        found = conn.execute(text(f"SELECT to_regclass('public.{table}')")).scalar()
        if found is None:
            return
    row = conn.execute(
        text("SELECT value FROM public.system_config WHERE key = :key"),
        {"key": KEY},
    ).first()
    value = row[0] if row is not None else None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or "tools" not in value:
        return
    tools = value["tools"]
    if isinstance(tools, list):
        names = [t for t in tools if isinstance(t, str) and t.strip()]
        for name in dict.fromkeys(names):
            if name not in ATOMIC_TOOLS:
                _bind(conn, name, EMULATOR)
        # A person unmarked Atomic's callable spelling: it stays unbound.
        if ATOMIC not in names:
            _bind(conn, ATOMIC, {})
    conn.execute(
        text(
            "UPDATE public.system_config SET value = value - 'tools' WHERE key = :key"
        ),
        {"key": KEY},
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
