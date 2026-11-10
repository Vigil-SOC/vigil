"""Join the two 0053 revisions, both written against 0052, into one head.

#4252 (erased assignees) and #4253 (get_case shell) landed side by side,
leaving two heads, and the API refuses to start on more than one. Neither
touches the other's tables, so there is nothing to run here.
"""

revision = "0054_merge_0053_heads"
down_revision = ("0053_erased_assignees", "0053_get_case_shell")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
