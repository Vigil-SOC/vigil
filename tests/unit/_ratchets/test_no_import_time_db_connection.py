"""Importing a router module must not open a database connection (#1456).

A module-level ``DatabaseDataService()`` used to run ``init_database(create_tables=True)``
at import, so a unit test importing a router connected to whatever Postgres the
developer had on localhost:5432 and could create tables in it. CI has no Postgres,
so only developer machines ever saw it.

Runs in a fresh interpreter: modules already in ``sys.modules`` would not re-execute
their top level, so an in-process import would prove nothing.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

_PROBE = textwrap.dedent("""
    import socket
    import sqlalchemy
    import sqlalchemy.engine

    class ImportTimeDatabaseAccess(BaseException):
        # BaseException: DatabaseDataService swallows Exception and logs a warning.
        pass

    def _forbidden(what):
        def _raise(*args, **kwargs):
            raise ImportTimeDatabaseAccess(f"import-time database access: {what}")
        return _raise

    sqlalchemy.create_engine = _forbidden("sqlalchemy.create_engine")
    sqlalchemy.engine.create_engine = _forbidden("sqlalchemy.engine.create_engine")
    socket.socket.connect = _forbidden("socket.connect")
    socket.socket.connect_ex = _forbidden("socket.connect_ex")
    socket.create_connection = _forbidden("socket.create_connection")

    import core.storage.connection as connection

    connection.init_database = _forbidden("init_database")
    connection.DatabaseManager.initialize = _forbidden("DatabaseManager.initialize")

    from services.api.discovery import load_router_specs

    print(len(load_router_specs()))
    """)


def test_importing_every_router_opens_no_database_connection():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        "a router module touches the database at import time; build the service "
        "lazily (DatabaseDataService connects on first use):\n" + result.stderr[-3000:]
    )
    assert int(result.stdout.strip().splitlines()[-1]) >= 40
