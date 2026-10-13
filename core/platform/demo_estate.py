"""The sample estate demo mode serves: one small fictional company, one day.

Example Corp is invented. Its addresses come from the RFC 5737 documentation
ranges and its names from the reserved ``example`` domains, so nothing here
points at a real host or organisation:

- 198.51.100.0/24 stands in for the office network and the VPN pool;
- 203.0.113.0/24 is the outside world: attackers and internet noise;
- 192.0.2.0/24 is the company's backup provider.

The day holds two attacks: a phished finance login used over the VPN to reach
the file server and then the domain controller, and an engineering workstation
beaconing to a lookalike CDN. Beside them are a password spray that failed, two
benign cases an analyst has closed, and noise that belongs to no case.

IDs are fixed and times are minutes before ``now``, so every process builds the
same estate and it always reads as the last day.
"""

import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, NamedTuple, Optional, Tuple

HOSTS = {
    "WS-FIN-07": "198.51.100.27",  # j.reyes, finance
    "WS-ENG-12": "198.51.100.42",  # m.okafor, engineering
    "WS-HR-03": "198.51.100.33",  # p.nair, HR
    "SRV-FILE-01": "198.51.100.10",
    "DC-01": "198.51.100.5",
    "JUMP-01": "198.51.100.8",  # the only host admins may RDP to DC-01 from
    "VPN-GW-01": "198.51.100.2",
    "SCAN-01": "198.51.100.250",  # vulnerability scanner
    "WEB-01": "198.51.100.80",
}
USERS = ("j.reyes", "m.okafor", "p.nair", "adm-tchen", "svc-backup")

VPN_LEASE = "198.51.100.201"  # the address the VPN gave the stolen session
PHONE = "198.51.100.140"  # office Wi-Fi
LURE_IP = "203.0.113.45"  # sent the lure, hosts the fake login, used the VPN
C2_IP = "203.0.113.77"
SPRAY_IP = "203.0.113.9"
SSH_NOISE_IP = "203.0.113.150"
BACKUP_IP = "192.0.2.80"

LURE_DOMAIN = "invoice-portal.example"
C2_DOMAIN = "cdn-telemetry.example"
BACKUP_DOMAIN = "backup.storage.example"

TECHNIQUES = {
    "T1003.001": "LSASS Memory",
    "T1021.001": "Remote Desktop Protocol",
    "T1021.002": "SMB/Windows Admin Shares",
    "T1041": "Exfiltration Over C2 Channel",
    "T1046": "Network Service Discovery",
    "T1053.005": "Scheduled Task",
    "T1056.003": "Web Portal Capture",
    "T1071.001": "Web Protocols",
    "T1071.004": "DNS",
    "T1078.002": "Domain Accounts",
    "T1110.001": "Password Guessing",
    "T1110.003": "Password Spraying",
    "T1133": "External Remote Services",
    "T1204.002": "Malicious File",
    "T1547.001": "Registry Run Keys / Startup Folder",
    "T1566": "Phishing",
    "T1566.002": "Spearphishing Link",
    "T1567.002": "Exfiltration to Cloud Storage",
    "T1569.002": "Service Execution",
    "T1621": "Multi-Factor Authentication Request Generation",
}


def _hash(name: str) -> str:
    """A stable, made-up SHA-256 for a sample file."""
    return hashlib.sha256(f"vigil-demo-estate:{name}".encode()).hexdigest()


def _ip(host: str) -> str:
    return HOSTS[host]


class _Finding(NamedTuple):
    finding_id: str
    minutes_ago: int
    data_source: str
    severity: str
    anomaly_score: float
    status: str
    cluster_id: Optional[str]
    title: str
    description: str
    techniques: Dict[str, float]  # technique id -> confidence
    entities: Dict[str, List[str]]


class _Case(NamedTuple):
    case_id: str
    title: str
    priority: str
    status: str
    assignee: str
    opened: int  # minutes before now
    updated: int
    story: str  # the tag beside "demo"
    description: str
    finding_ids: Tuple[str, ...]
    timeline: Tuple[Tuple[int, str], ...]  # (minutes before now, event)


# Tables, not black's one-value-per-line layout: a row per finding reads as a log.
# fmt: off
FINDINGS: Tuple[_Finding, ...] = (
    # Phished finance login used over the VPN
    _Finding(
        "f-demo-a1", 575, "email", "medium", 0.64, "investigating", "c-phish-vpn",
        f"Invoice lure linking to {LURE_DOMAIN} reached j.reyes",
        f"billing@{LURE_DOMAIN} sent 'Overdue invoice 4471' from {LURE_IP}. The link "
        "opens a copy of the company sign-in page. Three other finance mailboxes got "
        "it and did not click.",
        {"T1566.002": 0.91},
        {"usernames": ["j.reyes"], "src_ips": [LURE_IP], "domains": [LURE_DOMAIN]},
    ),
    _Finding(
        "f-demo-a2", 568, "proxy", "high", 0.82, "investigating", "c-phish-vpn",
        f"WS-FIN-07 posted a sign-in form to {LURE_DOMAIN}",
        "j.reyes opened the link 7 minutes after it arrived and submitted the form. "
        "The domain was first seen 2 days ago.",
        {"T1566.002": 0.86, "T1056.003": 0.74},
        {"hostnames": ["WS-FIN-07"], "usernames": ["j.reyes"],
         "src_ips": [_ip("WS-FIN-07")], "dest_ips": [LURE_IP], "domains": [LURE_DOMAIN]},
    ),
    _Finding(
        "f-demo-a3", 531, "siem", "high", 0.88, "investigating", "c-phish-vpn",
        f"VPN sign-in as j.reyes from {LURE_IP} after 6 MFA prompts",
        "Five pushes were denied and the sixth approved. The session came from the "
        "address that sent the lure, while WS-FIN-07 was on the office network. The "
        f"VPN leased it {VPN_LEASE}.",
        {"T1078.002": 0.83, "T1621": 0.79, "T1133": 0.66},
        {"hostnames": ["VPN-GW-01"], "usernames": ["j.reyes"], "src_ips": [LURE_IP],
         "dest_ips": [_ip("VPN-GW-01"), VPN_LEASE]},
    ),
    _Finding(
        "f-demo-a4", 506, "flow", "high", 0.84, "investigating", "c-phish-vpn",
        f"{VPN_LEASE} (j.reyes on VPN) opened admin shares on SRV-FILE-01",
        "SMB to ADMIN$ and C$, then a service install over the same session. "
        "j.reyes has never connected to SRV-FILE-01 before.",
        {"T1021.002": 0.87, "T1569.002": 0.58},
        {"hostnames": ["SRV-FILE-01"], "usernames": ["j.reyes"], "src_ips": [VPN_LEASE],
         "dest_ips": [_ip("SRV-FILE-01")]},
    ),
    _Finding(
        "f-demo-a5", 489, "edr", "critical", 0.96, "investigating", "c-phish-vpn",
        "LSASS memory read on SRV-FILE-01 by a renamed dump tool",
        "C:\\Windows\\Temp\\svch0st.exe, dropped by the service the VPN session "
        "installed, opened lsass.exe and wrote a 48 MB dump. adm-tchen's patch run "
        "had signed in here earlier.",
        {"T1003.001": 0.94},
        {"hostnames": ["SRV-FILE-01"], "usernames": ["j.reyes"],
         "file_hashes": [_hash("svch0st.exe")]},
    ),
    _Finding(
        "f-demo-a6", 462, "edr", "critical", 0.93, "new", "c-phish-vpn",
        "adm-tchen signed in to DC-01 over RDP from SRV-FILE-01",
        "Admins reach DC-01 only from JUMP-01. This sign-in came from SRV-FILE-01, "
        "27 minutes after its LSASS memory was read.",
        {"T1021.001": 0.9, "T1078.002": 0.81},
        {"hostnames": ["DC-01", "SRV-FILE-01"], "usernames": ["adm-tchen"],
         "src_ips": [_ip("SRV-FILE-01")], "dest_ips": [_ip("DC-01")]},
    ),
    # WS-ENG-12 beaconing
    _Finding(
        "f-demo-b1", 1318, "edr", "high", 0.79, "new", "c-beaconing",
        "Unsigned updater added to a Run key on WS-ENG-12",
        "%APPDATA%\\CdnHelper\\updater.exe came from a browser download and starts "
        "at sign-in. It is unsigned and no other host has it.",
        {"T1547.001": 0.88, "T1204.002": 0.61},
        {"hostnames": ["WS-ENG-12"], "usernames": ["m.okafor"],
         "file_hashes": [_hash("updater.exe")]},
    ),
    _Finding(
        "f-demo-b2", 1290, "dns", "medium", 0.71, "new", "c-beaconing",
        f"WS-ENG-12 resolves random subdomains of {C2_DOMAIN}",
        "About 60 lookups an hour, each a 32-character label. No other host queries "
        "the domain, which was registered 9 days ago.",
        {"T1071.004": 0.77},
        {"hostnames": ["WS-ENG-12"], "src_ips": [_ip("WS-ENG-12")],
         "domains": [C2_DOMAIN]},
    ),
    _Finding(
        "f-demo-b3", 1170, "flow", "high", 0.87, "new", "c-beaconing",
        f"WS-ENG-12 calls {C2_IP} every 60 s over HTTPS",
        f"Every 60 s with 2 s of jitter, kept up for hours with nobody signed in. "
        f"{C2_IP} serves {C2_DOMAIN}.",
        {"T1071.001": 0.89},
        {"hostnames": ["WS-ENG-12"], "usernames": ["m.okafor"],
         "src_ips": [_ip("WS-ENG-12")], "dest_ips": [C2_IP], "domains": [C2_DOMAIN]},
    ),
    _Finding(
        "f-demo-b4", 142, "proxy", "high", 0.90, "new", "c-beaconing",
        f"WS-ENG-12 sent 48 MB to {C2_DOMAIN} in 10 minutes",
        "The beacon usually sends under 1 KB a call. The upload started after "
        "m.okafor opened the source-code share.",
        {"T1041": 0.85},
        {"hostnames": ["WS-ENG-12"], "usernames": ["m.okafor"],
         "src_ips": [_ip("WS-ENG-12")], "dest_ips": [C2_IP], "domains": [C2_DOMAIN]},
    ),
    # Password spray that failed
    _Finding(
        "f-demo-c1", 880, "siem", "medium", 0.74, "investigating", "c-spray",
        f"Failed VPN sign-ins for 23 accounts from {SPRAY_IP} in 15 minutes",
        "One or two tries per account with seasonal passwords. None succeeded.",
        {"T1110.003": 0.86},
        {"hostnames": ["VPN-GW-01"], "usernames": ["p.nair", "m.okafor"],
         "src_ips": [SPRAY_IP], "dest_ips": [_ip("VPN-GW-01")]},
    ),
    _Finding(
        "f-demo-c2", 874, "siem", "low", 0.52, "investigating", "c-spray",
        f"p.nair locked out during the spray from {SPRAY_IP}",
        "The lockout came from the spray. p.nair signed in from WS-HR-03 after the "
        "help desk unlocked the account.",
        {"T1110.003": 0.70},
        {"hostnames": ["VPN-GW-01"], "usernames": ["p.nair"], "src_ips": [SPRAY_IP]},
    ),
    _Finding(
        "f-demo-c3", 866, "firewall", "low", 0.41, "resolved", "c-spray",
        f"Edge firewall now blocks {SPRAY_IP}",
        "The address crossed the failed sign-in threshold and was blocked.",
        {"T1110.003": 0.55},
        {"hostnames": ["VPN-GW-01"], "src_ips": [SPRAY_IP],
         "dest_ips": [_ip("VPN-GW-01")]},
    ),
    # Benign: the weekly vulnerability scan
    _Finding(
        "f-demo-d1", 1395, "flow", "medium", 0.67, "resolved", "c-scanner",
        "SCAN-01 touched 1,024 ports on 40 hosts in 20 minutes",
        "A sweep of the server range from the vulnerability scanner.",
        {"T1046": 0.81},
        {"hostnames": ["SCAN-01"], "src_ips": [_ip("SCAN-01")],
         "dest_ips": [_ip("SRV-FILE-01"), _ip("DC-01"), _ip("WEB-01")]},
    ),
    _Finding(
        "f-demo-d2", 1388, "firewall", "low", 0.39, "resolved", "c-scanner",
        "Internal firewall logged SCAN-01 probing DC-01",
        "Connection attempts to 312 ports on DC-01, all from SCAN-01.",
        {"T1046": 0.62},
        {"hostnames": ["SCAN-01", "DC-01"], "src_ips": [_ip("SCAN-01")],
         "dest_ips": [_ip("DC-01")]},
    ),
    # Benign: the daily backup
    _Finding(
        "f-demo-e1", 1095, "edr", "low", 0.44, "resolved", "c-backup",
        "svc-backup ran a scheduled task on SRV-FILE-01",
        "The daily backup task, on its usual schedule.",
        {"T1053.005": 0.57},
        {"hostnames": ["SRV-FILE-01"], "usernames": ["svc-backup"]},
    ),
    _Finding(
        "f-demo-e2", 1085, "flow", "medium", 0.69, "resolved", "c-backup",
        f"SRV-FILE-01 sent 38 GB to {BACKUP_DOMAIN}",
        "A large upload to cloud storage under svc-backup.",
        {"T1567.002": 0.73},
        {"hostnames": ["SRV-FILE-01"], "usernames": ["svc-backup"],
         "src_ips": [_ip("SRV-FILE-01")], "dest_ips": [BACKUP_IP],
         "domains": [BACKUP_DOMAIN]},
    ),
    # Noise that belongs to no case
    _Finding(
        "f-demo-n1", 1240, "endpoint", "low", 0.31, "resolved", None,
        "Adware bundle quarantined on WS-HR-03",
        "A free PDF tool came with an ad injector. Antivirus removed it.",
        {"T1204.002": 0.42},
        {"hostnames": ["WS-HR-03"], "usernames": ["p.nair"]},
    ),
    _Finding(
        "f-demo-n2", 980, "email", "low", 0.36, "resolved", None,
        "Bulk marketing mail from offers.example held as spam",
        "Sent to 41 mailboxes. No links were clicked.",
        {"T1566": 0.33},
        {"domains": ["offers.example"]},
    ),
    _Finding(
        "f-demo-n3", 720, "firewall", "low", 0.33, "resolved", None,
        "Internet SSH guessing at WEB-01 blocked",
        "Background noise: 900 attempts with common usernames, all refused.",
        {"T1110.001": 0.47},
        {"hostnames": ["WEB-01"], "src_ips": [SSH_NOISE_IP],
         "dest_ips": [_ip("WEB-01")]},
    ),
    _Finding(
        "f-demo-n4", 610, "edr", "low", 0.47, "resolved", None,
        "adm-tchen ran PsExec from JUMP-01 on SRV-FILE-01",
        "A patch run in the approved change window, from the admin host.",
        {"T1569.002": 0.52},
        {"hostnames": ["JUMP-01", "SRV-FILE-01"], "usernames": ["adm-tchen"],
         "src_ips": [_ip("JUMP-01")], "dest_ips": [_ip("SRV-FILE-01")]},
    ),
    _Finding(
        "f-demo-n5", 300, "siem", "low", 0.38, "new", None,
        "p.nair signed in from a new phone",
        "Office Wi-Fi, MFA passed, the usual mail and HR apps.",
        {"T1078.002": 0.35},
        {"usernames": ["p.nair"], "src_ips": [PHONE]},
    ),
    _Finding(
        "f-demo-n6", 95, "dns", "low", 0.34, "new", None,
        "WS-HR-03 resolved survey-tool.example, first seen today",
        "A survey link in an all-staff email. 30 hosts resolved it.",
        {"T1071.004": 0.31},
        {"hostnames": ["WS-HR-03"], "src_ips": [_ip("WS-HR-03")],
         "domains": ["survey-tool.example"]},
    ),
)

CASES: Tuple[_Case, ...] = (
    _Case(
        "case-demo-01",
        "Phished finance login used over VPN to reach SRV-FILE-01 and DC-01",
        "critical", "investigating", "soc-tier2@example.com", 528, 455, "phishing",
        "j.reyes submitted credentials to a lookalike invoice portal. Within the "
        f"hour {LURE_IP} signed in to the VPN as j.reyes, opened admin shares on "
        "SRV-FILE-01, dumped LSASS there and used adm-tchen to RDP to DC-01. Next: "
        "disable both accounts, isolate SRV-FILE-01 and check DC-01 for changes.",
        ("f-demo-a1", "f-demo-a2", "f-demo-a3", "f-demo-a4", "f-demo-a5", "f-demo-a6"),
        ((528, "Case opened from the VPN sign-in alert"),
         (500, "Linked admin-share access from the VPN session"),
         (485, "LSASS dump on SRV-FILE-01 raised priority to critical"),
         (455, "Linked adm-tchen's RDP sign-in to DC-01")),
    ),
    _Case(
        "case-demo-02", f"WS-ENG-12 beaconing to {C2_DOMAIN}",
        "high", "open", "", 1165, 140, "beaconing",
        "An unsigned updater on WS-ENG-12 starts at sign-in and calls "
        f"{C2_IP} every 60 s. Today it uploaded 48 MB. Next: isolate WS-ENG-12, "
        "collect updater.exe and list what m.okafor's session read.",
        ("f-demo-b1", "f-demo-b2", "f-demo-b3", "f-demo-b4"),
        ((1165, "Case opened from the 60 s beacon alert"),
         (140, "48 MB upload on the beacon channel linked")),
    ),
    _Case(
        "case-demo-03", f"Password spray against the VPN from {SPRAY_IP}",
        "medium", "investigating", "soc-tier1@example.com", 860, 850, "password-spray",
        "23 accounts tried and none got in. p.nair was locked out and is unlocked. "
        "The firewall blocks the address. Open: does the spray list match the "
        "finance mailboxes in the phishing case?",
        ("f-demo-c1", "f-demo-c2", "f-demo-c3"),
        ((860, "Case opened from the failed sign-in alert"),
         (850, "Firewall block confirmed")),
    ),
    _Case(
        "case-demo-04", "Port sweep from SCAN-01",
        "low", "closed", "soc-tier1@example.com", 1380, 1300, "scanner",
        "Closed as expected activity: SCAN-01 is the vulnerability scanner, and the "
        "sweep matches its weekly schedule and target list.",
        ("f-demo-d1", "f-demo-d2"),
        ((1380, "Case opened from the port sweep alert"),
         (1300, "Closed: scheduled vulnerability scan")),
    ),
    _Case(
        "case-demo-05", f"Large upload from SRV-FILE-01 to {BACKUP_DOMAIN}",
        "low", "closed", "soc-tier1@example.com", 1080, 1020, "backup",
        "Closed as expected activity: the daily backup. Same task, account, "
        "destination and size as the 30 days before, and hours before the "
        "intrusion on SRV-FILE-01 began.",
        ("f-demo-e1", "f-demo-e2"),
        ((1080, "Case opened from the cloud upload alert"),
         (1020, "Closed: daily backup")),
    ),
)
# fmt: on


def build_estate(now: datetime) -> Tuple[List[Dict], List[Dict]]:
    """Fresh findings (newest first) and cases for the estate, timed back from ``now``."""

    def at(minutes: int) -> str:
        return (now - timedelta(minutes=minutes)).isoformat()

    findings = [
        {
            "finding_id": spec.finding_id,
            "timestamp": at(spec.minutes_ago),
            "data_source": spec.data_source,
            "anomaly_score": spec.anomaly_score,
            "severity": spec.severity,
            "status": spec.status,
            "mitre_predictions": dict(spec.techniques),
            "predicted_techniques": [
                {"technique_id": t, "confidence": c, "technique_name": TECHNIQUES[t]}
                for t, c in spec.techniques.items()
            ],
            "entity_context": {k: list(v) for k, v in spec.entities.items()},
            "cluster_id": spec.cluster_id,
            "title": spec.title,
            "description": spec.description,
            "demo": True,
        }
        for spec in sorted(FINDINGS, key=lambda spec: spec.minutes_ago)
    ]
    techniques = {spec.finding_id: spec.techniques for spec in FINDINGS}
    cases = [
        {
            "case_id": spec.case_id,
            "title": spec.title,
            "description": spec.description,
            "finding_ids": list(spec.finding_ids),
            "status": spec.status,
            "priority": spec.priority,
            "assignee": spec.assignee,
            "tags": ["demo", spec.story],
            "mitre_techniques": sorted(
                {t for fid in spec.finding_ids for t in techniques[fid]}
            ),
            "notes": [],
            "timeline": [
                {"timestamp": at(ago), "event": event} for ago, event in spec.timeline
            ],
            "activities": [],
            "resolution_steps": [],
            "created_at": at(spec.opened),
            "updated_at": at(spec.updated),
            "demo": True,
        }
        for spec in CASES
    ]
    return findings, cases
