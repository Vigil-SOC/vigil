"""Join the two 0102 heads left on main into one.

0102_merge_0101_heads and 0102_unwrap_scalar_ioc_text landed side by side, and
the API refuses to start on more than one head. Neither is touched; this runs
nothing. A database at either upgrades through this, so the scalar-IOC rewrite
still runs for a database that only has the merge.
"""

revision = "0103_merge_0102_heads"
down_revision = (
    "0102_merge_0101_heads",
    "0102_unwrap_scalar_ioc_text",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
