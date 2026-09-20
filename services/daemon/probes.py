"""Known-answer probes (#923, epic #884).

Three synthetic findings the daemon feeds itself once a day so that store →
triage → LLM gateway is exercised end to end on a known input. A probe is a
Finding with ``data_source = "probe"`` whose known answer rides in
``entity_context["probe"]["expected"]``; it goes onto the processor's own input
queue like any polled finding and stops after triage (see the guard in
``FindingProcessor._enrich_in_background``). Scoring the answer is #924.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Dict, List

from core.time import utcnow

logger = logging.getLogger(__name__)

PROBE_DATA_SOURCE = "probe"

# Vocabulary _build_triage_prompt asks the model for; ``expected`` draws from it.
SEVERITIES = ("critical", "high", "medium", "low")
ACTIONS = ("isolate", "block", "investigate", "monitor", "dismiss")

# finding_id is String(50) and reads "probe:<name>:<YYYY-MM-DD>", so a name
# has 33 characters. Entities are RFC 5737 / RFC 2606, so an IP lookup can only
# come back empty; no file_hashes, so nothing reaches a sandbox or hash lookup.
PROBES: List[Dict[str, Any]] = [
    {
        "name": "c2-beacon-exfil",
        "description": (
            "Workstation ws-fin-17 opened an outbound TLS connection to "
            "203.0.113.77 every 60 seconds for six hours, each with a "
            "1.2 KB payload, then transferred 4.8 GB to the same host over "
            "port 443 at 02:14 local time. The destination is not in any "
            "approved vendor list and the host had earlier executed a "
            "PowerShell command with an encoded argument spawned from an "
            "Office document."
        ),
        "entity_context": {
            "src_ips": ["192.0.2.45"],
            "dest_ips": ["203.0.113.77"],
            "hostnames": ["ws-fin-17.example.com"],
            "usernames": ["j.doe"],
            "domains": ["update-check.example.net"],
        },
        "expected": {
            "severity": ["critical", "high"],
            "recommended_action": ["isolate", "block"],
        },
    },
    {
        "name": "routine-patch-window",
        "description": (
            "Service account svc-patch on jump host jump-01 pushed the monthly "
            "OS security updates to 42 servers in 198.51.100.0/24 over WinRM "
            "during the approved Tuesday 22:00-00:00 change window (CHG-4471). "
            "Every target rebooted once and reported healthy; no other "
            "activity from the account outside the window."
        ),
        "entity_context": {
            "src_ips": ["192.0.2.10"],
            "dest_ips": ["198.51.100.20"],
            "hostnames": ["jump-01.example.org"],
            "usernames": ["svc-patch"],
        },
        "expected": {
            "severity": ["low"],
            "recommended_action": ["dismiss", "monitor"],
        },
    },
    {
        "name": "impossible-travel-login",
        "description": (
            "User a.smith authenticated to the VPN from 198.51.100.200 at "
            "03:40, forty minutes after a successful badge-in at the London "
            "office; the source geolocates to a different continent. MFA "
            "succeeded, a mailbox forwarding rule was then created, and no "
            "further access has occurred. The account is a finance approver."
        ),
        "entity_context": {
            "src_ips": ["198.51.100.200"],
            "hostnames": ["vpn-gw-2.example.net"],
            "usernames": ["a.smith"],
        },
        "expected": {
            "severity": ["high", "medium"],
            "recommended_action": ["investigate"],
        },
    },
]


def probe_finding_id(name: str, day: date) -> str:
    return f"{PROBE_DATA_SOURCE}:{name}:{day.isoformat()}"


def build_probe_finding(probe: Dict[str, Any], day: date) -> Dict[str, Any]:
    """The finding dict the processor stores and triages. Only ``name`` and
    ``expected`` go under ``entity_context["probe"]``; #924 adds ``score``."""
    return {
        "finding_id": probe_finding_id(probe["name"], day),
        "data_source": PROBE_DATA_SOURCE,
        "timestamp": utcnow().isoformat(),
        "description": probe["description"],
        "entity_context": {
            **probe["entity_context"],
            "probe": {"name": probe["name"], "expected": probe["expected"]},
        },
    }


async def inject_probes(queue: asyncio.Queue, data_service: Any) -> int:
    """Queue today's probes that do not exist yet; return how many were queued.

    The id carries the day, so an hourly sweep (or a restart) re-injects
    nothing once the rows are stored — that gate is the "once a day".
    """
    day = utcnow().date()
    injected = 0
    for probe in PROBES:
        finding = build_probe_finding(probe, day)
        if data_service.get_finding(finding["finding_id"]):
            continue
        # Same envelope the poller and Kafka ingestor use.
        await queue.put(
            {
                "type": "finding",
                "source": PROBE_DATA_SOURCE,
                "data": finding,
                "timestamp": utcnow().isoformat(),
            }
        )
        injected += 1
    if injected:
        logger.info("Injected %d known-answer probe(s) for %s", injected, day)
    return injected
