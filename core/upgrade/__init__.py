"""Schema revisions, applied by ``python -m core.upgrade`` (see command.py).

Revisions live in ``versions/``: a thin .py per revision that runs the .sql
file beside it. Alembic is configured in code; there is no alembic.ini or
env.py, no autogenerate and no downgrade.
"""
