"""Day 1 Report records, insert-only for the app role (#4875).

``vigil_app`` is granted update and delete on a new table by default, so
this revision takes both away, as it does for ``agent_events``. A start and
its record are each inserted once and never rewritten.
"""

from core.upgrade.revision import run_sql

revision = "0108_report_records"
down_revision = "0107_merge_0106_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
