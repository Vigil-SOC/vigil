"""One head again: the feed cursor and the findings IP rewrite both followed 0005.

Neither depends on the other. This revision only joins them, so a database at
either head can take the other and ``stamp_schema_head`` has one revision to
record.
"""

revision = "0007_merge_heads"
down_revision = ("0006_audit_feed", "0006_canonical_finding_ips")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
