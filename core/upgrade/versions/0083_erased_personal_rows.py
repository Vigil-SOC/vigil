"""Drop the preferences row and blank the token labels of erased accounts.

Erasure now does both; this applies the same step, through
``retention.drop_personal_rows``, to each person erased on older code: those on
``$VIGIL_UPGRADE_DIR/erased-principals.txt`` and those already in the erased
state in the database. Running it again changes nothing.
"""

from alembic import op
from sqlalchemy import text

from core.audit import retention
from core.upgrade.revision import stash_report

revision = "0083_erased_personal_rows"
# After the merge revision, which landed beside this one on the same two heads.
down_revision = "0083_merge_0082_heads"


def upgrade() -> None:
    bind = op.get_bind()
    listed = retention.read_ids(retention.outside_path())
    # Erased state is the one is_erased tests: username = 'erased-' || principal_id.
    user_ids = (
        bind.execute(
            text("""
            SELECT user_id FROM public.users
            WHERE username = :prefix || principal_id
               OR principal_id = ANY(:listed)
            """),
            {"prefix": retention.ERASED_PREFIX, "listed": listed},
        )
        .scalars()
        .all()
    )
    for user_id in user_ids:
        retention.drop_personal_rows(bind, user_id)
    stash_report(revision, {"erased_accounts_cleaned": len(user_ids)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
