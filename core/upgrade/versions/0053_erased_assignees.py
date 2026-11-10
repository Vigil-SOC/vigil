"""Blank the case assignments that kept an erased assignee's username.

Erasure now also matches a trail row whose payload names the principal as
``assignee``; a case assignment or edit keeps that username beside the hash.
This re-blanks each principal already in ``$VIGIL_UPGRADE_DIR/erased-principals.txt``.
Nothing to do when it is unset.
"""

from alembic import op

from core.audit import retention, trail
from core.upgrade.revision import stash_report

revision = "0053_erased_assignees"
down_revision = "0052_api_tokens"


def upgrade() -> None:
    bind = op.get_bind()
    ids = retention.read_ids(retention.outside_path())
    if ids:
        trail._lock(bind)
    for principal_id in ids:
        retention._blank_beside(bind, principal_id)
    stash_report(revision, {"reblanked_assignees_of": len(ids)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
