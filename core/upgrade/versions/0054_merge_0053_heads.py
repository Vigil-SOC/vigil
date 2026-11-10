"""Join the two 0053 revisions, both written against 0052, into one head.

#4252 (erased assignees) and #4253 (get_case shell grant) landed side by side,
leaving two heads, and the API refuses to start on more than one. One blanks
case assignees, the other rewrites custom agent manifests, so neither depends
on the other and there is nothing to run here. The second was since renamed
0054_get_case_shell and chained after the first, so this follows the first
only; 0055_current_owners joins the heads.
"""

revision = "0054_merge_0053_heads"
down_revision = "0053_erased_assignees"


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
