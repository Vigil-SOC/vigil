"""Entity facts: what Entra, Okta and Falcon said about a finding's users and hosts (#4741).

One row per (Entity Key, source, Case or none), kept for its time to live and
read by ``core/integrations/entity_facts.py``. Nothing is copied or changed.
"""

from core.upgrade.revision import run_sql

revision = "0096_entity_facts"
down_revision = "0095_merge_0094_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
