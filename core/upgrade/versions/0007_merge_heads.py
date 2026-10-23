"""Join the two revisions that both follow 0005_agent_attribution.

0006_audit_feed and 0006_canonical_finding_ips landed side by side, so the
image had two Alembic heads and schema_head() refused to stamp a database.
This revision changes no schema. A database already on either branch runs
the other, then this.
"""

revision = "0007_merge_heads"
down_revision = ("0006_audit_feed", "0006_canonical_finding_ips")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
