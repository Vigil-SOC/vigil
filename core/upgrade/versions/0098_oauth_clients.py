"""OAuth clients, pending authorization requests, and grant columns (#4787).

Adds ``oauth_clients`` and ``oauth_pending``, and the grant columns on
``mcp_credentials`` (a grant is an ``McpCredential`` row, epic #4763 D4).
Nothing exists to copy: every new column is null and both tables start empty.
"""

from core.upgrade.revision import run_sql

revision = "0098_oauth_clients"
down_revision = "0097_sso_mfa_met"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
