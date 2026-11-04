"""Drop settings.read and settings.write from every role (#3747).

Nothing checks either key since #3324, and 0008 already copied their
successors, so this copies nothing. The report names each role that held
either key as true. Literals only: this does not read ``core.auth.permissions``.
"""

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import run_sql, stash_report

revision = "0030_drop_settings_keys"
down_revision = "0029_detection_rule_stages"


def upgrade() -> None:
    # Collect the holders before the SQL removes the keys.
    held = [row[0] for row in op.get_bind().execute(text("""
                SELECT name FROM public.roles
                WHERE permissions->>'settings.read' = 'true'
                   OR permissions->>'settings.write' = 'true'
                ORDER BY name
                """))]
    run_sql(__file__)
    stash_report(revision, {"removed_settings_keys_from": held})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
