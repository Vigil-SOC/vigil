"""Join the threat-indicator and skill-approval revisions into one head.

#4550 (threat-feed indicators) and #4509/#4510 (skill scans, then skills.approve)
landed side by side on 0080_merge_0079_heads, leaving two heads, and the API
refuses to start on more than one. Neither touches the other's tables, so there
is nothing to run here.
"""

revision = "0083_merge_0082_heads"
down_revision = ("0081_normalize_threat_indicators", "0082_skills_approve")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
