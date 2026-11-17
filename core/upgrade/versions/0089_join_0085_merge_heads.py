"""Join 0085_merge_heads with the rest of the tree.

#4577 landed 0085_merge_heads, a second join of the same three 0083/0084
revisions that 0085_join_0084_heads already joins, after the tree had moved on
to 0088_identifier_reservations. That leaves two heads, and the API refuses to
start on more than one. 0085_merge_heads runs no DDL, so there is nothing to
run here.
"""

revision = "0089_join_0085_merge_heads"
down_revision = ("0088_identifier_reservations", "0085_merge_heads")


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
