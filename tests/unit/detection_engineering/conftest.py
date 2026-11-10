"""Fixtures for the proposal tests (#4157), ported from 1.x's
``tests/unit/detection_engineering/conftest.py``.

They run on the real Postgres ``VIGIL_TEST_PG_DSN`` names, upgraded to head (export
POSTGRES_* to match), and skip without it. 1.x put invented rows in ``log_events``;
main reads the SIEMs, so ``siem`` stands in for one: events are added to it, each on
its Case's host, and ``siem_replay.run`` reads them through ``siem_replay.replay``
with the Case claims a ledger would give.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytest

DSN = os.environ.get("VIGIL_TEST_PG_DSN")

RULE = """
title: Office application starts encoded PowerShell
status: experimental
description: An Office application starts PowerShell with an encoded command.
level: high
tags:
  - attack.execution
  - attack.t1059.001
logsource:
  category: process_creation
  product: windows
detection:
  selection_parent:
    ParentImage|endswith:
      - '\\\\WINWORD.EXE'
      - '\\\\EXCEL.EXE'
  selection_child:
    Image|endswith: '\\\\powershell.exe'
    CommandLine|contains: '-enc'
  condition: selection_parent and selection_child
falsepositives:
  - Signed macros that call PowerShell
"""


@pytest.fixture
def db(monkeypatch):
    """The DSN's database, and no proposal or test Case left behind. The step record
    refuses DELETE, so it is truncated."""
    if not DSN:
        pytest.skip("VIGIL_TEST_PG_DSN not set")
    from sqlalchemy import text

    from core.storage import connection
    from core.storage.unit_of_work import unit_of_work

    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)
    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    connection.get_db_manager().initialize(force=True)

    def clean():
        with unit_of_work() as session:
            session.execute(
                text("TRUNCATE detection_proposal_events, detection_proposals")
            )
            session.execute(text("DELETE FROM cases WHERE case_id LIKE 'case-de-%'"))

    clean()
    yield
    clean()


@pytest.fixture
def add_case(db):
    from core.storage.models import Case
    from core.storage.unit_of_work import unit_of_work

    def add(case_id: str = "case-de-1") -> str:
        with unit_of_work() as session:
            session.add(
                Case(case_id=case_id, title="Macro to PowerShell on a test host")
            )
        return case_id

    return add


class _Siem:
    """One SIEM's events: (doc, case) pairs. A Case's events are on its own host."""

    def __init__(self):
        self.events: List[Tuple[Dict[str, Any], Optional[str]]] = []

    def add(
        self, raw: Dict[str, Any], *, case_id: Optional[str] = None, age_days: float = 1
    ) -> str:
        event_id = uuid.uuid4().hex
        at = datetime.now(timezone.utc) - timedelta(days=age_days)
        host = f"host-{case_id}" if case_id else f"host-normal-{len(self.events)}"
        doc = {
            "_id": event_id,
            "_source": {
                "@timestamp": at.isoformat(),
                "host": {"name": host},
                "winlog": {"event_data": raw},
            },
        }
        self.events.append((doc, case_id))
        return event_id


@pytest.fixture
def siem(monkeypatch):
    from core.detections.engineering import proposals, siem_replay

    fake = _Siem()

    class _Source(siem_replay.ElasticSource):
        def __init__(self, start, end):
            super().__init__(None, "test-events", 1000, "elastic-siem")
            self.start, self.end = start, end

        def spend(self, what):
            return True

        def _in(self, doc):
            at = siem_replay._when(doc["_source"]["@timestamp"])
            return self.start <= at <= self.end

        async def attack(self, claim, ids, fields, start, end):
            docs = [d for d, _ in fake.events if self._in(d)]
            if ids:
                docs = [d for d in docs if d["_id"] in ids]
            elif claim is not None:
                docs = [
                    d for d in docs if claim.holds(siem_replay.event_of("elastic", d))
                ]
            else:
                docs = []
            return siem_replay.Read(total=len(docs), docs=docs)

        async def benign(self, fields, start, end):
            docs = [d for d, _ in fake.events if self._in(d)]
            return siem_replay.Read(total=len(docs), docs=docs)

    async def run(
        sigma, *, case_id=None, named=None, days=siem_replay.WINDOW_DAYS, **_
    ):
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        claims = [
            siem_replay.claim_of(f"case:{c}", {"hostnames": [f"host-{c}"]}, start, end)
            for c in sorted({c for _, c in fake.events if c})
        ]
        attack = next((c for c in claims if c.ref == f"case:{case_id}"), None)
        if not fake.events:
            return await siem_replay.replay(
                sigma,
                sources=[],
                attack_claim=attack,
                named=named,
                claims=claims,
                start=start,
                end=end,
                skipped=["Elastic: elastic-siem is not enabled"],
            )
        return await siem_replay.replay(
            sigma,
            sources=[_Source(start, end)],
            attack_claim=attack,
            named=named,
            claims=claims,
            start=start,
            end=end,
        )

    monkeypatch.setattr(proposals.siem_replay, "run", run)
    return fake


@pytest.fixture
def attack_row():
    return {
        "ParentImage": "C:\\Program Files\\Office\\WINWORD.EXE",
        "Image": "C:\\Windows\\System32\\powershell.exe",
        "CommandLine": "powershell.exe -enc SQBFAFgA",
    }


@pytest.fixture
def signed_macro_row():
    return {
        "ParentImage": "C:\\Program Files\\Office\\EXCEL.EXE",
        "ParentCommandLine": "EXCEL.EXE \\\\files\\trusted-macros\\report.xlsm",
        "Image": "C:\\Windows\\System32\\powershell.exe",
        "CommandLine": "powershell.exe -enc UABpAG4AZwA=",
    }


@pytest.fixture
def plain_row():
    return {
        "ParentImage": "C:\\Windows\\explorer.exe",
        "Image": "C:\\Windows\\System32\\powershell.exe",
        "CommandLine": "powershell.exe -NoProfile",
    }
