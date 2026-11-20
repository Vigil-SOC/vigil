"""Join the labels revision with the history-import revision.

``0105_source_health_labels`` and ``0106_history_imports`` both follow
``0105_merge_0104_heads`` and landed side by side. The API refuses to start
on more than one head. Neither is touched; this runs nothing. A database at
either upgrades through this, so the labels column and the history-import
tables still appear for a database that only has the other.
"""

revision = "0107_merge_0106_heads"
down_revision = (
    "0105_source_health_labels",
    "0106_history_imports",
)


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
