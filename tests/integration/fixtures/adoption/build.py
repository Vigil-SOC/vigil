#!/usr/bin/env python3
"""Rebuild ``<tag>.sql``: a clean install of a release, dumped for the adoption tests.

Run from a ``git worktree`` of the tag, with this repo's virtualenv, against a
local Postgres where ``POSTGRES_USER`` is a superuser named ``vigil_fixture_owner``:

    git worktree add /tmp/wt-v0.7.0 v0.7.0
    POSTGRES_USER=vigil_fixture_owner POSTGRES_PASSWORD=... POSTGRES_PORT=... \\
        python tests/integration/fixtures/adoption/build.py /tmp/wt-v0.7.0 \\
        tests/integration/fixtures/adoption/v0.7.0.sql

The database is built the way that tag's ``scripts/generate_schema_snapshot.py``
builds one (init SQL, ``init_database()``, init SQL), then dumped as plain SQL
with INSERTs and no owner, and a varchar ``IN (...)`` written back in its source
form. The owner's name becomes ``__VIGIL_OWNER__`` so the
tests can load it as whichever role they connect as.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

OWNER = "vigil_fixture_owner"
PLACEHOLDER = "__VIGIL_OWNER__"


def main(worktree: str, out: str) -> int:
    if os.environ.get("POSTGRES_USER") != OWNER:
        sys.exit(
            f"set POSTGRES_USER={OWNER} (a superuser) so the dump can be rewritten"
        )
    os.chdir(worktree)
    sys.path.insert(0, worktree)
    from sqlalchemy import create_engine, text

    import scripts.generate_schema_snapshot as snap  # the tag's own copy

    name = "vigil_adoption_fixture"
    admin = snap._admin_engine()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {name}"))
    engine = create_engine(snap._url(name))
    try:
        snap._apply_init_sql(name)
        snap._init_on(engine)
        snap._apply_init_sql(name)
    finally:
        engine.dispose()

    env = dict(os.environ, PGPASSWORD=os.environ.get("POSTGRES_PASSWORD", ""))
    dump = subprocess.run(
        [
            "pg_dump",
            "--no-owner",
            "--inserts",
            "--quote-all-identifiers",
            "-h",
            os.environ.get("POSTGRES_HOST", "localhost"),
            "-p",
            os.environ.get("POSTGRES_PORT", "5432"),
            "-U",
            OWNER,
            name,
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()

    lines = [
        line
        for line in dump.splitlines()
        # psql meta-commands (\restrict) and settings an older server rejects.
        if not line.startswith("\\") and not line.startswith("SET transaction_timeout")
    ]
    body = re.sub(rf'"?\b{OWNER}\b"?', PLACEHOLDER, "\n".join(lines) + "\n")
    # As in 0001_baseline.sql: a varchar IN (...) goes back to its source form;
    # pg_dump's = ANY (ARRAY[...]) is stored differently once reloaded.
    body = re.sub(
        r'\("?(\w+)"?\)::"?text"? = ANY \(\(ARRAY\[([^\]]*)\]\)::"?text"?\[\]\)',
        lambda m: f'"{m[1]}" IN ({m[2].replace("::character varying", "")})',
        body,
    )
    Path(out).write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
