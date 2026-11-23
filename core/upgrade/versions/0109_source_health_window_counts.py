"""Per-window federation counts on the Integration Health record (#4940).

Adds ``source_health.returned_window``, ``stored_window`` and ``filtered_window``.
Null marks them uninitialized: the first successful federation poll resets all
four window counts, ``dropped_window`` included, under a fresh anchor.
"""

from core.upgrade.revision import run_sql

revision = "0109_source_health_window_counts"
down_revision = "0108_cases_export"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
