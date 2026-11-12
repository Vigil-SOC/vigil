"""Join the two 0065 revisions, both written against 0064, into one head.

#4348 (end unscoped runs) and #4340 (Medic watch) landed side by side,
leaving two heads, and the API refuses to start on more than one. Neither
touches the other's tables, so there is nothing to run here.
"""

revision = "0066_merge_0065_heads"
down_revision = ("0065_end_unscoped_runs", "0065_medic_watch")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
