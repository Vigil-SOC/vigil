"""Clear invite traces still tied to accounts erased before this revision.

Erasure now blanks the address and source address on a person's invite entries
and closes an invite still open for an address they hold. This applies that
scrub, through ``retention.scrub_invites``, to each person erased on older
code: those on ``$VIGIL_UPGRADE_DIR/erased-principals.txt`` and those already
in the erased state. Only links, trail rows and open invites still reachable
from the stored principal are touched, and the rewritten ``erased-…`` address
is skipped. The inviter's notices stay. Running it again changes nothing.
"""

from alembic import op
from sqlalchemy import text

from core.audit import retention
from core.upgrade.revision import stash_report

revision = "0101_erased_invites"
down_revision = "0100_shared_ioc_case_only"


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
    # One pass: every open invite is locked before the first trail append.
    retention.scrub_invites(bind, principal_ids, notices=False)
    stash_report(revision, {"erased_invites_scrubbed": len(principal_ids)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
