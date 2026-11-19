"""Delete notices still addressed to accounts erased before this revision.

A restore now deletes the notices addressed to a person erased since the
backup, the same delete erasure already ran. This applies that delete, through
``retention._drop_notices``, to each person erased on older code: those on
``$VIGIL_UPGRADE_DIR/erased-principals.txt`` and those already in the erased
state. Notices other people received about them stay. The username has already
been rewritten, so those rows cannot be matched. Running it again changes
nothing.
"""

from alembic import op
from sqlalchemy import text

from core.audit import retention
from core.upgrade.revision import stash_report

revision = "0104_erased_notices"
down_revision = "0103_merge_0102_heads"


def upgrade() -> None:
    bind = op.get_bind()
    listed = retention.read_ids(retention.outside_path())
    # Erased state is the one is_erased tests: username = 'erased-' || principal_id.
    principal_ids = list(
        bind.execute(
            text("""
                SELECT principal_id FROM public.users
                WHERE username = :prefix || principal_id
                   OR principal_id = ANY(CAST(:listed AS text[]))
                """),
            {"prefix": retention.ERASED_PREFIX, "listed": listed},
        ).scalars()
    )
    for principal_id in principal_ids:
        retention._drop_notices(bind, principal_id)
    stash_report(revision, {"erased_notices_dropped": len(principal_ids)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
