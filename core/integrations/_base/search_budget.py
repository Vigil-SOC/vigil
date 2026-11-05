"""How many searches an integration's SIEM has been sent in the last hour.

Every check that searches a SIEM (Kibana and Wazuh Sees, and Quiet, on the
elastic-siem budget; Splunk Runs and Sees on the splunk one) calls ``spend``
before each search, with the limit
its descriptor's ``max_searches_per_hour`` resolves to. Listing rules and
metadata calls (``_find``, ``_field_caps``) are not searches and do not spend.

The window is the ``siem_searches`` table, so it is one per integration across
every Vigil process (the daemon's sync, the API's reload, any worker) and it
survives a restart. Count and record are one step under a transaction-scoped
advisory lock per integration, timed by the database's clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, Optional

from sqlalchemy import text

from core.storage.connection import get_db_manager

WINDOW_SECONDS = 3600
# A keyed row outlives the window as its key's last search, up to this long.
KEY_DAYS = 30
# First key of pg_advisory_xact_lock(int, int): "SIEM"; the second is the integration.
_LOCK_SPACE = 0x5349_454D

_SPEND = (
    text("SELECT pg_advisory_xact_lock(:space, hashtext(:i))"),
    # Out of the window: unkeyed rows, and keyed ones a later search of the key supersedes.
    text(f"""
        DELETE FROM siem_searches s
        WHERE s.integration_id = :i
          AND s.searched_at <= clock_timestamp() - interval '{WINDOW_SECONDS} seconds'
          AND (s.search_key IS NULL
               OR s.searched_at <= clock_timestamp() - interval '{KEY_DAYS} days'
               OR EXISTS (SELECT 1 FROM siem_searches n
                          WHERE n.integration_id = s.integration_id
                            AND n.search_key = s.search_key
                            AND n.searched_at > s.searched_at))
    """),
)
_COUNT = text(f"""
    SELECT count(*) FROM siem_searches
    WHERE integration_id = :i
      AND searched_at > clock_timestamp() - interval '{WINDOW_SECONDS} seconds'
""")
_RECORD = text("""
    INSERT INTO siem_searches (integration_id, search_key, searched_at)
    VALUES (:i, :key, clock_timestamp())
""")


def spend(integration_id: str, per_hour: int, key: Optional[str] = None) -> bool:
    """Count one search against the hour, or False when the budget is spent.

    ``key`` names what is searched, for callers that order by ``last_spent``.
    """
    params = {"i": integration_id, "space": _LOCK_SPACE, "key": key}
    with get_db_manager().session_scope() as session:
        for statement in _SPEND:
            session.execute(statement, params)
        if session.execute(_COUNT, params).scalar_one() >= per_hour:
            return False
        session.execute(_RECORD, params)
        return True


def last_spent(integration_id: str, keys: Iterable[str]) -> Dict[str, datetime]:
    """When each key was last searched, by any process; a key never searched is absent."""
    keys = list(keys)
    if not keys:
        return {}
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            text("""
                SELECT search_key, max(searched_at) FROM siem_searches
                WHERE integration_id = :i AND search_key = ANY(:keys)
                GROUP BY search_key
            """),
            {"i": integration_id, "keys": keys},
        ).all()
    return {k: at for k, at in rows}
