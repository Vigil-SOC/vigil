"""Email addresses already on file count as confirmed.

Widens ``auth_links.kind`` with ``email`` (a 24-hour confirmation of an
address the person changed) and marks every existing account's email
confirmed. A person's own later change is what leaves an address unconfirmed.
Does not edit an earlier revision.
"""

from core.upgrade.revision import run_sql

revision = "0077_email_confirmed"
down_revision = "0076_merge_0075_heads"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
