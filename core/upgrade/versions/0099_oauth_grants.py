"""What an OAuth grant and its approval are bound to (#4788).

Adds ``mcp_credentials.resource``, so ``/mcp`` can refuse a token issued for a
different resource, and ``oauth_pending.sign_in_id``, so a code dies with the
sign-in that approved it. Both null where they do not apply; nothing to copy.
"""

from core.upgrade.revision import run_sql

revision = "0099_oauth_grants"
down_revision = "0098_oauth_clients"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
