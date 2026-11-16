"""Join the directory-pending and email-confirmation revisions into one head.

#4507 (directory pushes waiting for a link) and #4445 (confirm a changed
email) landed side by side, leaving two heads, and the API refuses to start
on more than one. Neither touches the other's tables, so there is nothing
to run here.
"""

revision = "0078_merge_0077_heads"
down_revision = ("0077_directory_pending", "0077_email_confirmed")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
