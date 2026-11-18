"""The Case is the only subject of the shared IOC index (#4815, epic #4580).

Deletes the legacy Investigation rows, drops ``shared_iocs.investigation_id`` and
makes ``case_id`` and ``cases.indicators_synced_at`` NOT NULL. ``plan()`` refuses
the upgrade while any Case is unsynced (``unsynced_cases_refusal`` in
``core/upgrade/command.py``), so every Case's own rows are already in place.
"""

from core.upgrade.revision import run_sql

revision = "0100_shared_ioc_case_only"
down_revision = "0099_oauth_grants"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
