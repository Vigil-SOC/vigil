"""Join the two heads left when #4468 and #4485 landed side by side.

``0074_approval_held`` and ``0075_directory_groups`` (through
``0074_directory_sync``) both follow ``0073_medic_fix_notices``. The API
refuses to start on more than one head. Neither revision touches the other's
columns, so there is nothing to run here.
"""

revision = "0076_merge_0075_heads"
down_revision = ("0074_approval_held", "0075_directory_groups")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
