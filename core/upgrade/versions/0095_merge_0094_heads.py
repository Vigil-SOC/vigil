"""Join the two 0094 heads left on main into one.

0094_case_indicator_catchup_idx and 0094_merge_0093_heads both descend from the two
0093 heads and landed side by side, and the API refuses to start on more than one head.
Neither is touched; this runs nothing. A database at either upgrades through this.
"""

revision = "0095_merge_0094_heads"
down_revision = (
    "0094_case_indicator_catchup_idx",
    "0094_merge_0093_heads",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
