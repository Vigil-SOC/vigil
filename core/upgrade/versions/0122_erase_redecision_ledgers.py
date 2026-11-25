"""Erase marked re-decision ledgers, and grant that act to built-in Admin.

``agent_events`` stays insert-only for ``vigil_app`` and ``vigil_agent``.
``erase_redecision_ledgers`` is the only delete, and only of runs whose
``trigger_context`` is a re-decision. ``ledgers.erase`` is written as a
literal so a later catalog edit does not change who already holds it.
"""

from core.upgrade.revision import run_sql

revision = "0122_erase_redecision_ledgers"
down_revision = "0121_history_ticket_alert_ids"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
