"""Sample alerts a fresh install can load to see triage work before any data
source is connected.

They are ordinary findings in the database, so the daemon triages them like
any other alert (its enrichment backfill picks up stored findings that were
never triaged). Every one carries ``data_source = SAMPLE_DATA_SOURCE`` and is
cleared by that mark, so a load and a clear never touch real alerts.

The set mixes a short intrusion story with the routine noise a SOC sees, at
every severity, so the queue shows both verdicts. Addresses are from the
documentation ranges (RFC 5737) and names use ``example.corp``.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.time import utcnow

SAMPLE_DATA_SOURCE = "vigil-sample"

# (minutes before load, fields). finding_id and external_id are fixed, so a
# second load while the first is still there skips instead of duplicating.
_SAMPLES: List[Dict[str, Any]] = [
    {
        "key": "phish-click",
        "minutes_ago": 118,
        "severity": "medium",
        "title": "User clicked a link in a reported phishing email",
        "description": (
            "j.ortiz@example.corp opened hxxps://invoice-portal.example.net/view "
            "from an email that two other users reported as phishing. The proxy "
            "allowed the request."
        ),
        "anomaly_score": 0.71,
        "mitre": {"T1566.002": 0.86, "T1204.001": 0.64},
        "entities": {
            "usernames": ["j.ortiz"],
            "hostnames": ["fin-lt-0142"],
            "src_ips": ["192.0.2.41"],
            "domains": ["invoice-portal.example.net"],
        },
    },
    {
        "key": "impossible-travel",
        "minutes_ago": 96,
        "severity": "high",
        "title": "Sign-in from two countries 12 minutes apart",
        "description": (
            "j.ortiz signed in from 198.51.100.23 (Lisbon) twelve minutes after a "
            "sign-in from the office network. MFA was approved on the second "
            "sign-in."
        ),
        "anomaly_score": 0.88,
        "mitre": {"T1078.004": 0.81},
        "entities": {
            "usernames": ["j.ortiz"],
            "src_ips": ["198.51.100.23", "192.0.2.41"],
        },
    },
    {
        "key": "inbox-rule",
        "minutes_ago": 90,
        "severity": "high",
        "title": "New inbox rule forwards finance mail to an outside address",
        "description": (
            "A rule created on j.ortiz's mailbox moves mail containing "
            "'invoice' or 'payment' to RSS Feeds and forwards it to "
            "billing-desk@example.net."
        ),
        "anomaly_score": 0.9,
        "mitre": {"T1114.003": 0.92, "T1564.008": 0.73},
        "entities": {
            "usernames": ["j.ortiz"],
            "src_ips": ["198.51.100.23"],
            "domains": ["example.net"],
        },
    },
    {
        "key": "encoded-powershell",
        "minutes_ago": 74,
        "severity": "critical",
        "title": "Encoded PowerShell launched by Excel",
        "description": (
            "EXCEL.EXE started powershell.exe -enc on fin-lt-0142. The decoded "
            "command downloads a script from 203.0.113.80 and runs it in memory."
        ),
        "anomaly_score": 0.97,
        "mitre": {"T1059.001": 0.95, "T1027": 0.82, "T1105": 0.77},
        "entities": {
            "usernames": ["j.ortiz"],
            "hostnames": ["fin-lt-0142"],
            "dest_ips": ["203.0.113.80"],
            "processes": ["EXCEL.EXE", "powershell.exe"],
        },
    },
    {
        "key": "backup-admin-share",
        "minutes_ago": 62,
        "severity": "low",
        "title": "Admin share access from the backup server",
        "description": (
            "svc-backup on bkp-01 read C$ on 14 file servers during the nightly "
            "backup window, as it does every night."
        ),
        "anomaly_score": 0.32,
        "mitre": {"T1021.002": 0.41},
        "entities": {
            "usernames": ["svc-backup"],
            "hostnames": ["bkp-01"],
            "src_ips": ["192.0.2.10"],
        },
    },
    {
        "key": "vuln-scan",
        "minutes_ago": 48,
        "severity": "low",
        "title": "Port scan from the vulnerability scanner",
        "description": (
            "192.0.2.15 (scan-01) probed 1,024 ports across the server subnet. "
            "The scan matches the weekly scheduled job."
        ),
        "anomaly_score": 0.28,
        "mitre": {"T1046": 0.52},
        "entities": {"hostnames": ["scan-01"], "src_ips": ["192.0.2.15"]},
    },
    {
        "key": "failed-logins",
        "minutes_ago": 31,
        "severity": "medium",
        "title": "Burst of failed sign-ins on a service account",
        "description": (
            "svc-reporting failed to sign in 46 times in five minutes from "
            "app-03. The password was rotated this morning and the job still "
            "uses the old one."
        ),
        "anomaly_score": 0.55,
        "mitre": {"T1110.001": 0.58},
        "entities": {
            "usernames": ["svc-reporting"],
            "hostnames": ["app-03"],
            "src_ips": ["192.0.2.33"],
        },
    },
    {
        "key": "large-upload",
        "minutes_ago": 12,
        "severity": "high",
        "title": "15 MB upload to a newly registered domain",
        "description": (
            "fin-lt-0142 uploaded 15.2 MB over HTTPS to files.example.net, a "
            "domain first seen today, shortly after the PowerShell download."
        ),
        "anomaly_score": 0.84,
        "mitre": {"T1567.002": 0.79, "T1041": 0.61},
        "entities": {
            "hostnames": ["fin-lt-0142"],
            "dest_ips": ["203.0.113.80"],
            "domains": ["files.example.net"],
        },
    },
]

SAMPLE_COUNT = len(_SAMPLES)


def sample_finding_id(key: str) -> str:
    return f"sample-{key}"


def build_sample_alerts(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Rows for ``DatabaseService.bulk_create_findings``, timed back from ``now``."""
    now = now or utcnow()
    rows = []
    for sample in _SAMPLES:
        rows.append(
            {
                "finding_id": sample_finding_id(sample["key"]),
                "external_id": sample["key"],
                "data_source": SAMPLE_DATA_SOURCE,
                "timestamp": now - timedelta(minutes=sample["minutes_ago"]),
                "severity": sample["severity"],
                "title": sample["title"],
                "description": sample["description"],
                "anomaly_score": sample["anomaly_score"],
                "mitre_predictions": dict(sample["mitre"]),
                "entity_context": {k: list(v) for k, v in sample["entities"].items()},
            }
        )
    return rows
