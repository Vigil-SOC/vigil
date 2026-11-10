"""Grant the case shell where a custom agent named get_case (#4239).

``get_case`` is no longer offered to agents; they read a case through
``read_case_shell`` (capability ``shell``). A stored manifest whose ``needs``
(top level or a role's) names it gets ``shell`` in its place, and no second
``shell`` if it already has one; ``shell`` stays in ``optional`` only if all it
replaces was optional. The manifest is parsed and dumped, so a rewritten one
loses its comments; one that does not name it is left as stored. The report
lists rewritten ids. Copied from 0049 rather than shared.
"""

from typing import Any, List

import yaml
from alembic import op
from sqlalchemy import text

from core.upgrade.revision import stash_report

revision = "0054_get_case_shell"
down_revision = "0053_erased_assignees"

_DELETED = frozenset({"get_case"})
_SHELL = "shell"


class _BlockDumper(yaml.SafeDumper):
    """Multi-line strings as | blocks, so a prompt stays readable."""


_BlockDumper.add_representer(
    str,
    lambda dumper, value: dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    ),
)


def _granted(needs: Any) -> Any:
    """``needs`` with the deleted names folded into one shell, or as it was."""
    if not isinstance(needs, list) or not any(n in _DELETED for n in needs):
        return needs
    out: List[Any] = []
    for need in needs:
        need = _SHELL if need in _DELETED else need
        if need == _SHELL and _SHELL in out:
            continue
        out.append(need)
    return out


def _rewrite(manifest: str) -> Any:
    """The new manifest text, or None when it names no deleted tool."""
    try:
        doc = yaml.safe_load(manifest)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    changed = False
    roles = doc.get("roles")
    holders = [doc] + [
        role
        for role in (roles.values() if isinstance(roles, dict) else ())
        if isinstance(role, dict)
    ]
    for holder in holders:
        needs = holder.get("needs")
        granted = _granted(needs)
        if granted is needs:
            continue
        holder["needs"], changed = granted, True
        # optional must stay within needs: shell is optional only when every
        # name it replaces (and any shell already there) was optional.
        optional = holder.get("optional")
        if isinstance(optional, list):
            required = any(
                n not in optional for n in needs if n in _DELETED or n == _SHELL
            )
            optional = _granted(optional)
            holder["optional"] = [n for n in optional if not (required and n == _SHELL)]
    if not changed:
        return None
    return yaml.dump(
        doc, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True, width=1000
    )


def upgrade() -> None:
    bind = op.get_bind()
    rewritten = []
    for agent_id, manifest in bind.execute(
        text("SELECT id, manifest FROM custom_agents ORDER BY id")
    ):
        new = _rewrite(manifest or "")
        if new is None:
            continue
        bind.execute(
            text(
                "UPDATE custom_agents SET manifest = :m, updated_at = now() WHERE id = :id"
            ),
            {"m": new, "id": agent_id},
        )
        rewritten.append(agent_id)
    stash_report(revision, {"granted_shell_to": rewritten})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
