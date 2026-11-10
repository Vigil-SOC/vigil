"""Free the username and email of accounts erased before #4225.

Erasure now rewrites them to ``erased-<principal_id>``; this applies the same
rewrite, through ``retention.free_identifiers``, to each principal already in
``$VIGIL_UPGRADE_DIR/erased-principals.txt``. Nothing to do when it is unset.
"""

from alembic import op

from core.audit import retention
from core.upgrade.revision import stash_report

revision = "0051_erased_identifiers"
down_revision = "0050_step_up"


def upgrade() -> None:
    bind = op.get_bind()
    ids = retention.read_ids(retention.outside_path())
    for principal_id in ids:
        retention.free_identifiers(bind, principal_id)
    stash_report(revision, {"freed_identifiers_of": len(ids)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
