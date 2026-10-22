"""What a revision file calls: run the .sql file beside it."""

from pathlib import Path

from alembic import op


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
