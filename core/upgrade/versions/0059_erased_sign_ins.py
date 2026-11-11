"""Blank the browser, device and address kept on an erased person's sign-ins.

Erasure now blanks ``user_agent``, ``device`` and ``address`` on the person's
``sign_ins`` rows (#4182); this blanks them, in one pass, for each principal
already in ``$VIGIL_UPGRADE_DIR/erased-principals.txt``. The rows and their ids
stay. Nothing to do when it is unset.
"""

from alembic import op
from sqlalchemy import text

from core.audit import retention
from core.upgrade.revision import stash_report

revision = "0059_erased_sign_ins"
down_revision = "0058_sso_links"


def upgrade() -> None:
    ids = retention.read_ids(retention.outside_path())
    if ids:
        op.get_bind().execute(
            text(
                "UPDATE public.sign_ins SET user_agent = NULL, device = NULL,"
                " address = NULL WHERE principal_id = ANY(:ids)"
            ),
            {"ids": list(ids)},
        )
    stash_report(revision, {"blanked_sign_ins_of": len(ids)})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
