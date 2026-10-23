"""One head again after the audit feed and canonical finding IPs both
followed 0005."""

revision = "0007_join_schema_heads"
down_revision = ("0006_audit_feed", "0006_canonical_finding_ips")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
