"""Join the break-glass, threat-indicator and skill-scan revisions into one head.

#4532 (break-glass accounts), #4537 (threat indicators) and #4509/#4510
(skill scans and approve) landed side by side, leaving three heads, and the API
refuses to start on more than one. None touches another's tables, so there is
nothing to run here.
"""

revision = "0083_merge_0081_heads"
down_revision = (
    "0081_break_glass",
    "0081_normalize_threat_indicators",
    "0082_skills_approve",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
