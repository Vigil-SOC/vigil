"""One head again.

``0006_audit_feed`` and ``0006_canonical_finding_ips`` both followed
``0005_agent_attribution``. The image serves a single head, so this revision
joins them and changes no schema.
"""

revision = "0007_join_heads"
down_revision = ("0006_audit_feed", "0006_canonical_finding_ips")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
