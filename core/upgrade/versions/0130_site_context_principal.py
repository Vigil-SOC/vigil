"""The site context agent's principal (#5259).

Chains after ``0129_pan_unblock_ip_class``.
"""

from core.upgrade.revision import run_sql

revision = "0130_site_context_principal"
down_revision = "0129_pan_unblock_ip_class"


def upgrade() -> None:
    run_sql(__file__)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
