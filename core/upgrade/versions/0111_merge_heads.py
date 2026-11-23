"""Join the three heads left on main into one.

``0108_info_below_low``, ``0108_report_records`` and
``0110_workflow_run_standing`` landed side by side, and the API refuses to
start on more than one head. None of them is touched; this runs nothing. A
database at any of them upgrades through this.
"""

revision = "0111_merge_heads"
down_revision = (
    "0108_info_below_low",
    "0108_report_records",
    "0110_workflow_run_standing",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
