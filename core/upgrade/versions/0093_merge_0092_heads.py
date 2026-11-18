"""Join the two 0092 heads left on main into one.

0092_security_lake_tool_classes and 0092_token_reminded_at landed side by side, and
the API refuses to start on more than one head. Neither is touched; this runs nothing.
A database at either upgrades through this.
"""

revision = "0093_merge_0092_heads"
down_revision = (
    "0092_security_lake_tool_classes",
    "0092_token_reminded_at",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
