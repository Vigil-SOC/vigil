"""Join the two 0093 heads left on main into one.

0093_case_indicator_index and 0093_merge_0092_heads both descend from the two 0092
heads and landed side by side, and the API refuses to start on more than one head.
Neither is touched; this runs nothing. A database at either upgrades through this.
"""

revision = "0094_merge_0093_heads"
down_revision = (
    "0093_case_indicator_index",
    "0093_merge_0092_heads",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
