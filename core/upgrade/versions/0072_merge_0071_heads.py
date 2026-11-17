"""Join the two 0071 revisions, both written against 0070, into one head.

#4399 (false-positive cause) and #4397 (exception citations) landed side by
side, leaving two heads, and the API refuses to start on more than one.
Neither touches the other's columns, so there is nothing to run here.
"""

revision = "0072_merge_0071_heads"
down_revision = ("0071_exception_citations", "0071_false_positive_cause")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
