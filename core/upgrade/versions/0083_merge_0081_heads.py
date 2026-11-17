"""Join the agent-role, threat-indicator and skill-approval revisions into one head.

0081_agent_role, 0081_normalize_threat_indicators and 0081_skill_scans (with
0082_skills_approve after it) landed side by side on 0080_merge_0079_heads,
leaving three heads, and the API refuses to start on more than one. None of
them touches another's tables, so there is nothing to run here.
"""

revision = "0083_merge_0081_heads"
down_revision = (
    "0081_agent_role",
    "0081_normalize_threat_indicators",
    "0082_skills_approve",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
