"""Join the recovery end-reason and lockout-threshold revisions into one head.

#4449 (recovery and MFA-reset end reasons) and #4450 (lockout threshold 3–10)
landed side by side, leaving two heads, and the API refuses to start on more
than one. Neither touches the other's tables, so there is nothing to run here.
"""

revision = "0080_merge_0079_heads"
down_revision = ("0079_lockout_threshold", "0079_recovery_mfa_reset")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
