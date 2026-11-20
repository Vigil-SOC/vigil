"""History imports get their own record (#4886, epic #4885).

Adds ``history_imports`` (one row per import, with its run status) and
``history_tickets`` (every kept ticket of an import, bulk-closed included),
backfills ``history_imports`` from the imported-kind markers in
``episodic_distil_markers`` and deletes those markers.
"""

from core.upgrade.revision import run_sql

revision = "0106_history_imports"
down_revision = "0105_merge_0104_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
