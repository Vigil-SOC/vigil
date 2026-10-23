"""One head: the audit-feed revision and the canonical-IP revision both
follow 0005, and the image serves a single revision."""

revision = "0007_merge_heads"
down_revision = ("0006_audit_feed", "0006_canonical_finding_ips")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
