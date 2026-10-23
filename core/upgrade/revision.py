"""What a revision file calls: run the .sql file beside it.

A revision that has something to say calls ``stash_report``. ``python -m
core.upgrade`` puts whatever was stashed into the one history record.
"""

from pathlib import Path

from alembic import op

_reports: list[dict] = []
# Revision ids whose version row was updated. A report stashed before that
# update is dropped: the migration transaction rolls the change back with it.
_stamped: list[str] = []


def stash_report(revision_id: str, report: dict) -> None:
    """Remember ``report`` until the upgrade command records the run."""
    _reports.append({"revision": revision_id, "report": report})


def note_stamped(revision_id: str) -> None:
    """The version row now names ``revision_id``."""
    _stamped.append(revision_id)


def take_reports() -> list[dict]:
    """Stashed reports whose revision was stamped, oldest first."""
    stamped = set(_stamped)
    found = [item for item in _reports if item["revision"] in stamped]
    _reports.clear()
    _stamped.clear()
    return found


def run_sql(revision_file: str) -> None:
    sql = Path(revision_file).with_suffix(".sql").read_text(encoding="utf-8")
    # The raw DBAPI cursor: no bind-parameter parsing, so a '%' or ':name' in
    # the SQL stays literal, and psycopg2 runs the whole file in one call.
    cursor = op.get_bind().connection.cursor()
    try:
        cursor.execute(sql)
        # A SET in one file must not outlive it into the next revision.
        cursor.execute("RESET ALL")
    finally:
        cursor.close()
