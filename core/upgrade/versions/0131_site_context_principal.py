"""The site context agent's principal (#5259).

Chains after ``0130_cf_gateway_unblock_class``.
"""

from core.upgrade.revision import run_sql

revision = "0131_site_context_principal"
down_revision = "0130_cf_gateway_unblock_class"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
