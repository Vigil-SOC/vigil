"""Store each case and task owner as its account's username, once (#4263).

A stored assignee is current only when it is exactly an active person's
username, so an owner left as an email or another casing (``ALICE``) is
rewritten to the username of the one non-service account it names, by username
or email ignoring case, whatever that account's state. A row older than that
account is skipped: the value was written before the account held the name.
No owner changes, so no trail entry and no ``updated_at`` bump. The report
counts rewritten rows. Also joins the three 0054 heads.
"""

from alembic import op
from sqlalchemy import text

from core.upgrade.revision import stash_report

revision = "0055_current_owners"
down_revision = "0054_security_keys"

# {table} and {key} are fixed names below, never input.
_REWRITE = """
UPDATE {table} AS t SET assignee = one.username
FROM (
    SELECT r.{key} AS id, min(u.username) AS username, min(p.created_at) AS created_at
    FROM {table} r
    JOIN users u
      ON u.service_account IS NOT TRUE
     AND lower(r.assignee) IN (lower(u.username), lower(u.email))
    JOIN principals p ON p.principal_id = u.principal_id
    WHERE r.assignee IS NOT NULL
    GROUP BY r.{key}
    HAVING count(*) = 1
) AS one
WHERE t.{key} = one.id
  AND t.assignee <> one.username
  AND one.created_at <= t.updated_at
"""


def upgrade() -> None:
    bind = op.get_bind()
    report = {
        table: bind.execute(text(_REWRITE.format(table=table, key=key))).rowcount
        for table, key in (("cases", "case_id"), ("case_tasks", "task_id"))
    }
    stash_report(revision, {"rewritten_owners": report})


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
