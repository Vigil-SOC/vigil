"""Join the two 0101 heads left on main into one.

0101_erased_invites and 0101_sso_required_end_reason both descend from
0100_shared_ioc_case_only and landed side by side, and the API refuses to start
on more than one head. Neither is touched; this runs nothing. A database at
either upgrades through this.
"""

revision = "0102_merge_0101_heads"
down_revision = (
    "0101_erased_invites",
    "0101_sso_required_end_reason",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
