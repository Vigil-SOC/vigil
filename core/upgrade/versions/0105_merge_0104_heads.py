"""Join the two 0104 heads left on main into one.

0104_erased_notices and 0104_source_health landed side by side, and the API
refuses to start on more than one head. Neither is touched; this runs nothing.
A database at either upgrades through this, so the source-health copy and the
erased-notice delete still run for a database that only has the other.
"""

revision = "0105_merge_0104_heads"
down_revision = (
    "0104_erased_notices",
    "0104_source_health",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
