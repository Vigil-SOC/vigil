"""Curated demo scenarios: alerts as real integrations surface them.

Two groups, told apart by the state signals Vigil already reads rather than a
new field:

* Routine alerts the triage agent settles alone. Each is noise-marked by the
  agent (dismissed) and carries a triage block that recommends ``dismiss`` or
  ``monitor``.
* Advanced findings that always need a person. Each sits in an open case and is
  triaged ``escalate`` or ``investigate``, so demo mode lists it under Needs you.

Sources are Wazuh (IP blocks, user authentication), LibreNMS (SNMP login
failures) and a NetBox lookup that has verified, or failed to verify, a
device's IP and MAC. NetBox is not a finding source here: its verdict is
recorded under ``entity_context["netbox"]`` on the finding it enriched.

Everything is a pure function of ``now``, so the data is the same on every
call and carries no randomness.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

AUTO = "auto"
HUMAN = "human_review"

# ATT&CK technique names for the ids these scenarios use.
SCENARIO_TECHNIQUES = {
    "T1110.001": "Password Guessing",
    "T1110.003": "Password Spraying",
    "T1078": "Valid Accounts",
    "T1078.002": "Domain Accounts",
    "T1021.001": "Remote Desktop Protocol",
    "T1021.004": "SSH",
    "T1046": "Network Service Discovery",
    "T1602.001": "SNMP (MIB Dump)",
    "T1557.002": "ARP Cache Poisoning",
    "T1136.001": "Local Account",
    "T1548.003": "Sudo and Sudo Caching",
    "T1562.004": "Disable or Modify System Firewall",
    "T1098": "Account Manipulation",
}


def _netbox(
    *,
    ip: str,
    mac: str,
    device: Optional[str] = None,
    role: Optional[str] = None,
    site: Optional[str] = None,
    ip_known: bool,
    mac_known: bool,
    mac_on_record: Optional[str] = None,
) -> Dict[str, Any]:
    """A NetBox lookup of the source's IP and MAC.

    ``verified`` is true only when NetBox has the IP and the MAC seen on the
    wire is the one recorded against it.
    """
    matches = ip_known and mac_known and (mac_on_record or mac).lower() == mac.lower()
    return {
        "queried_ip": ip,
        "observed_mac": mac,
        "ip_in_netbox": ip_known,
        "mac_in_netbox": mac_known,
        "recorded_mac": (
            mac_on_record if mac_on_record else (mac if mac_known else None)
        ),
        "verified": matches,
        "device": device,
        "role": role,
        "site": site,
    }


def _triage(
    kind: str,
    category: str,
    action: str,
    confidence: float,
    reasoning: str,
    when: datetime,
) -> Dict[str, Any]:
    """The daemon's stored triage, in the shape ``findings.ai_enrichment`` holds."""
    stamp = when.isoformat()
    return {
        "category": category,
        "ai_triage": {
            "result": {
                "category": category,
                "severity": "low" if kind == AUTO else "high",
                "reasoning": reasoning,
                "confidence": confidence,
                "recommended_action": action,
            },
            "timestamp": stamp,
        },
        "enriched_at": stamp,
        "triage_reasoning": reasoning,
        "triage_confidence": confidence,
        "recommended_action": action,
    }


def _finding(
    *,
    key: str,
    now: datetime,
    minutes_ago: int,
    source: str,
    kind: str,
    title: str,
    description: str,
    severity: str,
    score: float,
    techniques: Dict[str, float],
    entities: Dict[str, Any],
    category: str,
    action: str,
    confidence: float,
    reasoning: str,
    external_id: str,
    dismissed_reason: Optional[str] = None,
) -> Dict[str, Any]:
    when = now - timedelta(minutes=minutes_ago)
    finding: Dict[str, Any] = {
        "finding_id": f"f-demo-{key}",
        "external_id": external_id,
        "timestamp": when.isoformat(),
        "data_source": source,
        "title": title,
        "description": description,
        "severity": severity,
        "anomaly_score": score,
        "mitre_predictions": techniques,
        "entity_context": entities,
        "cluster_id": None,
        "ai_enrichment": _triage(
            kind, category, action, confidence, reasoning, when + timedelta(seconds=40)
        ),
        "scenario": kind,
        "demo": True,
    }
    if kind == AUTO:
        finding["noise_marked"] = True
        finding["noise_marked_at"] = (when + timedelta(minutes=1)).isoformat()
        finding["noise_marked_by"] = "triage-agent"
        finding["noise_marked_by_kind"] = "agent"
        finding["noise_reason"] = dismissed_reason or reasoning
    return finding


def _case(
    *,
    key: str,
    now: datetime,
    minutes_ago: int,
    title: str,
    description: str,
    priority: str,
    tags: List[str],
    finding_ids: List[str],
) -> Dict[str, Any]:
    created = now - timedelta(minutes=minutes_ago)
    return {
        "case_id": f"case-demo-{key}",
        "title": title,
        "description": description,
        "finding_ids": finding_ids,
        "status": "open",
        "priority": priority,
        "assignee": "",
        "tags": ["demo", *tags],
        "notes": [],
        "timeline": [
            {"timestamp": created.isoformat(), "event": "Case created (demo)"}
        ],
        "activities": [],
        "resolution_steps": [],
        "created_at": created.isoformat(),
        "updated_at": created.isoformat(),
        "demo": True,
    }


def build_scenarios(now: datetime) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(findings, cases)``: routine alerts first, then the advanced incidents."""
    f: List[Dict[str, Any]] = []

    # ---- Routine: the triage agent settles these without a person ----------------
    f.append(
        _finding(
            key="wz-ipblock-scanner",
            now=now,
            minutes_ago=95,
            source="wazuh",
            kind=AUTO,
            title="Host Blocked by firewall-drop Active Response",
            description=(
                "Wazuh rule 651: firewall-drop blocked 45.148.10.121 on edge-fw-01 "
                "after 8 failed SSH logins in 60 s. No login succeeded."
            ),
            severity="low",
            score=0.31,
            techniques={"T1110.001": 0.72},
            entities={
                "src_ips": ["45.148.10.121"],
                "hostnames": ["edge-fw-01"],
                "wazuh": {
                    "rule_id": "651",
                    "level": 3,
                    "decoder": "ossec",
                    "action": "firewall-drop",
                    "block_seconds": 600,
                    "blocked": True,
                },
            },
            category="policy_violation",
            action="dismiss",
            confidence=0.97,
            reasoning=(
                "Active response already blocked the scanner. No successful "
                "authentication followed and the source is a known mass-scan range."
            ),
            external_id="wazuh-651-45.148.10.121",
        )
    )
    f.append(
        _finding(
            key="wz-ipblock-repeat",
            now=now,
            minutes_ago=62,
            source="wazuh",
            kind=AUTO,
            title="Host Blocked by firewall-drop Active Response",
            description=(
                "Wazuh rule 651: firewall-drop blocked 176.53.159.198 on web-02 "
                "after repeated invalid-user SSH attempts."
            ),
            severity="low",
            score=0.28,
            techniques={"T1110.001": 0.7},
            entities={
                "src_ips": ["176.53.159.198"],
                "hostnames": ["web-02"],
                "wazuh": {
                    "rule_id": "651",
                    "level": 3,
                    "decoder": "ossec",
                    "action": "firewall-drop",
                    "block_seconds": 600,
                    "blocked": True,
                },
            },
            category="policy_violation",
            action="dismiss",
            confidence=0.96,
            reasoning="Blocked on first threshold crossing; usernames tried never existed.",
            external_id="wazuh-651-176.53.159.198",
        )
    )
    f.append(
        _finding(
            key="wz-auth-typo",
            now=now,
            minutes_ago=180,
            source="wazuh",
            kind=AUTO,
            title="User login failed, then succeeded",
            description=(
                "Wazuh rule 5503: two failed logins for jlee on ws-114 from the office "
                "VLAN, then a successful login 20 s later."
            ),
            severity="low",
            score=0.12,
            techniques={"T1078": 0.4},
            entities={
                "src_ips": ["10.20.4.58"],
                "hostnames": ["ws-114"],
                "usernames": ["jlee"],
                "wazuh": {
                    "rule_id": "5503",
                    "level": 5,
                    "decoder": "pam",
                    "failed_attempts": 2,
                    "then_success": True,
                },
            },
            category="benign_user_error",
            action="dismiss",
            confidence=0.95,
            reasoning=(
                "Two failures then a success from the user's usual workstation and "
                "subnet is a mistyped password."
            ),
            external_id="wazuh-5503-jlee-ws114",
        )
    )
    f.append(
        _finding(
            key="wz-auth-lockout-known",
            now=now,
            minutes_ago=240,
            source="wazuh",
            kind=AUTO,
            title="User account locked out",
            description="Wazuh rule 5551: account svc-backup locked out after stale credentials on nas-01.",
            severity="medium",
            score=0.22,
            techniques={"T1110.001": 0.45},
            entities={
                "src_ips": ["10.20.9.12"],
                "hostnames": ["nas-01"],
                "usernames": ["svc-backup"],
                "wazuh": {
                    "rule_id": "5551",
                    "level": 10,
                    "decoder": "pam",
                    "locked_out": True,
                },
                "netbox": _netbox(
                    ip="10.20.9.12",
                    mac="3c:ec:ef:11:22:33",
                    device="bkp-agent-03",
                    role="Backup server",
                    site="HQ",
                    ip_known=True,
                    mac_known=True,
                ),
            },
            category="misconfiguration",
            action="monitor",
            confidence=0.9,
            reasoning=(
                "Source is a NetBox-verified backup server (IP and MAC match). The "
                "service account password was rotated yesterday; a job still uses the old one."
            ),
            external_id="wazuh-5551-svc-backup",
        )
    )
    f.append(
        _finding(
            key="lnms-snmp-poller",
            now=now,
            minutes_ago=130,
            source="librenms",
            kind=AUTO,
            title="SNMP authentication failure",
            description=(
                "LibreNMS: SNMP v2c community mismatch from 10.20.1.5 against sw-core-02 "
                "(14 failures in 5 min)."
            ),
            severity="low",
            score=0.2,
            techniques={"T1110.001": 0.3},
            entities={
                "src_ips": ["10.20.1.5"],
                "hostnames": ["sw-core-02"],
                "librenms": {
                    "device_id": 14,
                    "protocol": "snmp",
                    "version": "v2c",
                    "event": "snmp_auth_failed",
                    "failures": 14,
                },
                "netbox": _netbox(
                    ip="10.20.1.5",
                    mac="52:54:00:ab:01:05",
                    device="nms-01",
                    role="Network monitoring",
                    site="HQ",
                    ip_known=True,
                    mac_known=True,
                ),
            },
            category="misconfiguration",
            action="dismiss",
            confidence=0.94,
            reasoning=(
                "Source is the LibreNMS poller itself, verified in NetBox. The failures "
                "start right after the community string rotation on sw-core-02."
            ),
            external_id="librenms-snmp-sw-core-02-poller",
        )
    )
    f.append(
        _finding(
            key="lnms-snmp-labswitch",
            now=now,
            minutes_ago=75,
            source="librenms",
            kind=AUTO,
            title="SNMP authentication failure",
            description="LibreNMS: one SNMP v2c auth failure on ap-lab-07 from 10.20.30.41.",
            severity="low",
            score=0.17,
            techniques={"T1110.001": 0.25},
            entities={
                "src_ips": ["10.20.30.41"],
                "hostnames": ["ap-lab-07"],
                "librenms": {
                    "device_id": 41,
                    "protocol": "snmp",
                    "version": "v2c",
                    "event": "snmp_auth_failed",
                    "failures": 1,
                },
                "netbox": _netbox(
                    ip="10.20.30.41",
                    mac="dc:a6:32:5e:90:11",
                    device="sw-lab-12",
                    role="Access switch",
                    site="Lab",
                    ip_known=True,
                    mac_known=True,
                ),
            },
            category="benign_user_error",
            action="dismiss",
            confidence=0.92,
            reasoning=(
                "A single failure from a NetBox-verified lab switch (IP and MAC match "
                "its record), no repeat in 24 h."
            ),
            external_id="librenms-snmp-ap-lab-07",
        )
    )
    f.append(
        _finding(
            key="wz-auth-verified-device",
            now=now,
            minutes_ago=50,
            source="wazuh",
            kind=AUTO,
            title="Multiple failed logins in a small period of time",
            description=(
                "Wazuh rule 5551: 4 failed SSH logins for mchen on jump-01 from "
                "10.20.7.33 over 3 minutes. Last login succeeded."
            ),
            severity="medium",
            score=0.35,
            techniques={"T1110.001": 0.55},
            entities={
                "src_ips": ["10.20.7.33"],
                "hostnames": ["jump-01"],
                "usernames": ["mchen"],
                "wazuh": {
                    "rule_id": "5551",
                    "level": 10,
                    "decoder": "pam",
                    "failed_attempts": 4,
                    "then_success": True,
                },
                "netbox": _netbox(
                    ip="10.20.7.33",
                    mac="a4:83:e7:2f:10:9c",
                    device="mchen-laptop",
                    role="Workstation",
                    site="HQ",
                    ip_known=True,
                    mac_known=True,
                ),
            },
            category="benign_user_error",
            action="dismiss",
            confidence=0.9,
            reasoning=(
                "Source IP and MAC both match mchen's assigned laptop in NetBox, on the "
                "corporate VLAN, and the final attempt succeeded."
            ),
            external_id="wazuh-5551-mchen-jump01",
        )
    )

    # ---- Advanced: always a human decision -----------------------------------------
    f.append(
        _finding(
            key="wz-brute-then-success",
            now=now,
            minutes_ago=34,
            source="wazuh",
            kind=HUMAN,
            title="Multiple authentication failures followed by a success",
            description=(
                "Wazuh rule 40112: 213 failed SSH logins for root and admin on bastion-01 "
                "from 109.160.32.113, then a successful root login at 03:12 UTC."
            ),
            severity="critical",
            score=0.97,
            techniques={"T1110.001": 0.96, "T1078": 0.88, "T1021.004": 0.8},
            entities={
                "src_ips": ["109.160.32.113"],
                "hostnames": ["bastion-01"],
                "usernames": ["root", "admin"],
                "wazuh": {
                    "rule_id": "40112",
                    "level": 12,
                    "decoder": "pam",
                    "failed_attempts": 213,
                    "then_success": True,
                },
            },
            category="intrusion",
            action="escalate",
            confidence=0.95,
            reasoning=(
                "A long failure run ended in a successful root login from the same "
                "external IP. Treat as a compromise until proven otherwise; isolation "
                "and credential reset are person decisions."
            ),
            external_id="wazuh-40112-109.160.32.113",
        )
    )
    f.append(
        _finding(
            key="wz-ipblock-failed",
            now=now,
            minutes_ago=31,
            source="wazuh",
            kind=HUMAN,
            title="Active response failed: firewall-drop did not apply",
            description=(
                "Wazuh rule 652: firewall-drop for 109.160.32.113 on bastion-01 returned "
                "an error; the iptables rule was not inserted and the attacker kept connecting."
            ),
            severity="high",
            score=0.9,
            techniques={"T1562.004": 0.7, "T1110.001": 0.85},
            entities={
                "src_ips": ["109.160.32.113"],
                "hostnames": ["bastion-01"],
                "wazuh": {
                    "rule_id": "652",
                    "level": 9,
                    "decoder": "ossec",
                    "action": "firewall-drop",
                    "blocked": False,
                    "error": "iptables: Resource temporarily unavailable",
                },
            },
            category="intrusion",
            action="escalate",
            confidence=0.9,
            reasoning=(
                "The automatic block failed while an attacker with a working root "
                "session is connected. The firewall must be fixed by hand."
            ),
            external_id="wazuh-652-109.160.32.113",
        )
    )
    f.append(
        _finding(
            key="lnms-snmp-sweep",
            now=now,
            minutes_ago=58,
            source="librenms",
            kind=HUMAN,
            title="SNMP authentication failures across multiple devices",
            description=(
                "LibreNMS: 9 devices logged SNMP auth failures from 10.20.99.77, then a "
                "v2c read with community 'public' succeeded on sw-edge-05."
            ),
            severity="high",
            score=0.91,
            techniques={"T1046": 0.84, "T1110.003": 0.78, "T1602.001": 0.8},
            entities={
                "src_ips": ["10.20.99.77"],
                "hostnames": ["sw-edge-05", "sw-edge-06", "rtr-wan-01"],
                "librenms": {
                    "protocol": "snmp",
                    "version": "v2c",
                    "devices_hit": 9,
                    "event": "snmp_auth_failed",
                    "then_success": True,
                    "community_guessed": "public",
                },
                "netbox": _netbox(
                    ip="10.20.99.77",
                    mac="de:ad:be:ef:00:77",
                    ip_known=False,
                    mac_known=False,
                ),
            },
            category="reconnaissance",
            action="escalate",
            confidence=0.92,
            reasoning=(
                "An address with no NetBox record swept management SNMP and read a switch "
                "with the default community. A rogue host on the management network "
                "needs a person to locate and contain."
            ),
            external_id="librenms-snmp-sweep-10.20.99.77",
        )
    )
    f.append(
        _finding(
            key="wz-auth-mac-mismatch",
            now=now,
            minutes_ago=44,
            source="wazuh",
            kind=HUMAN,
            title="Failed logins from a device whose MAC does not match NetBox",
            description=(
                "Wazuh rule 5551: 11 failed logins for finance-svc on app-03 from "
                "10.20.8.61. NetBox lists 10.20.8.61 as printer-fin-02, but the MAC on "
                "the wire is not the printer's."
            ),
            severity="high",
            score=0.88,
            techniques={"T1557.002": 0.74, "T1110.003": 0.7, "T1078": 0.6},
            entities={
                "src_ips": ["10.20.8.61"],
                "hostnames": ["app-03"],
                "usernames": ["finance-svc"],
                "wazuh": {
                    "rule_id": "5551",
                    "level": 10,
                    "decoder": "pam",
                    "failed_attempts": 11,
                    "then_success": False,
                },
                "netbox": _netbox(
                    ip="10.20.8.61",
                    mac="00:0c:29:9a:3e:b1",
                    device="printer-fin-02",
                    role="Printer",
                    site="HQ",
                    ip_known=True,
                    mac_known=False,
                    mac_on_record="00:80:77:31:6a:02",
                ),
            },
            category="intrusion",
            action="investigate",
            confidence=0.85,
            reasoning=(
                "The IP belongs to a printer, but another MAC is using it and trying a "
                "service-account password. That looks like an impersonated address "
                "(ARP or static-IP takeover) and needs a person to trace the port."
            ),
            external_id="wazuh-5551-finance-svc-10.20.8.61",
        )
    )
    f.append(
        _finding(
            key="wz-new-admin-sudo",
            now=now,
            minutes_ago=29,
            source="wazuh",
            kind=HUMAN,
            title="New local account added and granted sudo",
            description=(
                "Wazuh rules 5902 and 5402: account 'svc-update' created on bastion-01 "
                "17 s after the root login, then added to the sudoers group."
            ),
            severity="critical",
            score=0.95,
            techniques={"T1136.001": 0.93, "T1548.003": 0.85, "T1098": 0.7},
            entities={
                "src_ips": ["109.160.32.113"],
                "hostnames": ["bastion-01"],
                "usernames": ["root", "svc-update"],
                "wazuh": {
                    "rule_id": "5902",
                    "level": 8,
                    "decoder": "ossec",
                    "event": "user_added",
                    "sudoers_changed": True,
                },
            },
            category="persistence",
            action="escalate",
            confidence=0.94,
            reasoning=(
                "A new privileged account right after an attacker-owned root session is "
                "persistence. Removing it and rotating credentials is a person decision."
            ),
            external_id="wazuh-5902-svc-update",
        )
    )
    f.append(
        _finding(
            key="wz-rdp-domain-admin",
            now=now,
            minutes_ago=20,
            source="wazuh",
            kind=HUMAN,
            title="Domain admin RDP logon from an external address",
            description=(
                "Wazuh rule 60106 (Windows 4624 type 10): domain admin d.okafor logged "
                "on to dc-01 over RDP from 203.0.113.58 outside business hours."
            ),
            severity="critical",
            score=0.93,
            techniques={"T1021.001": 0.9, "T1078.002": 0.87},
            entities={
                "src_ips": ["203.0.113.58"],
                "hostnames": ["dc-01"],
                "usernames": ["d.okafor"],
                "wazuh": {
                    "rule_id": "60106",
                    "level": 12,
                    "decoder": "windows_eventchannel",
                    "logon_type": 10,
                    "business_hours": False,
                },
            },
            category="intrusion",
            action="escalate",
            confidence=0.9,
            reasoning=(
                "A domain admin session from outside the corporate ranges, at night, on "
                "a domain controller. Only a person can confirm with the account owner."
            ),
            external_id="wazuh-60106-d.okafor-dc01",
        )
    )

    by_key = {x["finding_id"]: x for x in f}
    cases = [
        _case(
            key="bastion-compromise",
            now=now,
            minutes_ago=28,
            title="Possible compromise of bastion-01 from 109.160.32.113",
            description=(
                "Brute force, successful root login, a failed firewall block and a new "
                "sudo account, all from the same external IP."
            ),
            priority="critical",
            tags=["brute-force", "persistence", "wazuh"],
            finding_ids=[
                "f-demo-wz-brute-then-success",
                "f-demo-wz-ipblock-failed",
                "f-demo-wz-new-admin-sudo",
            ],
        ),
        _case(
            key="snmp-management-sweep",
            now=now,
            minutes_ago=55,
            title="Rogue host sweeping management SNMP (10.20.99.77)",
            description="Unknown address, not in NetBox, guessed the default SNMP community.",
            priority="high",
            tags=["reconnaissance", "snmp", "librenms", "netbox"],
            finding_ids=["f-demo-lnms-snmp-sweep"],
        ),
        _case(
            key="ip-mac-conflict",
            now=now,
            minutes_ago=42,
            title="IP 10.20.8.61 used by a device that is not printer-fin-02",
            description="NetBox records the printer's MAC; a different MAC is brute-forcing finance-svc.",
            priority="high",
            tags=["spoofing", "netbox", "wazuh"],
            finding_ids=["f-demo-wz-auth-mac-mismatch"],
        ),
        _case(
            key="dc-external-rdp",
            now=now,
            minutes_ago=18,
            title="Domain admin RDP from external address 203.0.113.58",
            description="Domain admin logon to dc-01 from outside the corporate ranges.",
            priority="critical",
            tags=["valid-accounts", "rdp", "wazuh"],
            finding_ids=["f-demo-wz-rdp-domain-admin"],
        ),
    ]
    assert all(fid in by_key for c in cases for fid in c["finding_ids"])
    return f, cases
