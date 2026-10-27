"""Join the two 0010 revisions, both written against 0009, into one head.

#3200 (autonomy ceiling) and #3205 (command bar search indexes) landed side by
side, leaving two heads, and the API refuses to start on more than one. Neither
touches the other's tables, so there is nothing to run here.
"""

revision = "0011_merge_0010_heads"
down_revision = ("0010_autonomy_ceiling", "0010_command_bar_search_indexes")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
