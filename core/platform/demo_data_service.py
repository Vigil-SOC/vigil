"""Demo data service for Vigil SOC demo mode."""

import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.platform.demo_estate import build_estate

logger = logging.getLogger(__name__)

MITRE_TECHNIQUES = {
    "T1566.001": "Spearphishing Attachment",
    "T1566.002": "Spearphishing Link",
    "T1059.001": "PowerShell",
    "T1059.003": "Windows Command Shell",
    "T1071.001": "Web Protocols",
    "T1071.004": "DNS",
    "T1021.001": "Remote Desktop Protocol",
    "T1021.002": "SMB/Windows Admin Shares",
    "T1078.001": "Default Accounts",
    "T1078.002": "Domain Accounts",
    "T1003.001": "LSASS Memory",
    "T1003.002": "Security Account Manager",
    "T1055.001": "DLL Injection",
    "T1055.012": "Process Hollowing",
    "T1486": "Data Encrypted for Impact",
    "T1490": "Inhibit System Recovery",
    "T1047": "WMI",
    "T1053.005": "Scheduled Task",
    "T1547.001": "Registry Run Keys",
    "T1574.001": "DLL Search Order Hijacking",
    "T1041": "Exfiltration Over C2 Channel",
    "T1567.002": "Exfiltration to Cloud Storage",
    "T1027": "Obfuscated Files",
    "T1070.004": "File Deletion",
}

DATA_SOURCES = ["flow", "edr", "siem", "dns", "proxy", "firewall", "email", "endpoint"]
CLUSTERS = [
    "c-beaconing-001",
    "c-lateral-movement-002",
    "c-exfiltration-003",
    "c-credential-theft-004",
    "c-ransomware-005",
    "c-phishing-006",
    "c-c2-communication-007",
    "c-persistence-008",
]
SEVERITIES = ["critical", "high", "medium", "low"]
SEVERITY_WEIGHTS = [0.1, 0.25, 0.4, 0.25]


def generate_ip() -> str:
    if random.random() < 0.6:
        return f"192.168.{random.randint(1, 254)}.{random.randint(1, 254)}"
    return f"{random.randint(1, 223)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"


def generate_hostname() -> str:
    prefixes = ["ws", "srv", "dc", "web", "db", "app", "dev", "prod"]
    return f"{random.choice(prefixes)}-{random.randint(1, 999):03d}"


def generate_username() -> str:
    names = ["john", "jane", "bob", "alice", "admin", "service", "backup"]
    return f"{random.choice(names)}{random.randint(1, 99)}"


def generate_mitre_predictions() -> Dict[str, float]:
    num_techniques = random.randint(1, 4)
    techniques = random.sample(list(MITRE_TECHNIQUES.keys()), num_techniques)
    return {tech: round(random.uniform(0.5, 0.99), 3) for tech in techniques}


def generate_entity_context() -> Dict[str, Any]:
    context = {}
    if random.random() < 0.8:
        context["src_ips"] = [generate_ip() for _ in range(random.randint(1, 3))]
    if random.random() < 0.6:
        context["dest_ips"] = [generate_ip() for _ in range(random.randint(1, 2))]
    if random.random() < 0.7:
        context["hostnames"] = [
            generate_hostname() for _ in range(random.randint(1, 2))
        ]
    if random.random() < 0.5:
        context["usernames"] = [generate_username()]
    if random.random() < 0.3:
        context["file_hashes"] = [uuid.uuid4().hex for _ in range(random.randint(1, 2))]
    return context


class DemoDataService:
    """The sample estate (``demo_estate``) that demo mode serves, held in memory.

    The random helpers above are kept for ``scripts/generate_sample_data.py``.
    """

    _instance = None
    _findings: List[Dict] = []
    _cases: List[Dict] = []
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not DemoDataService._initialized:
            self._generate_demo_data()
            DemoDataService._initialized = True

    def _generate_demo_data(self):
        """Load the sample estate, timed back from now."""
        findings, cases = build_estate(datetime.now(timezone.utc))
        logger.info(
            "Loading the demo sample estate: %d findings, %d cases",
            len(findings),
            len(cases),
        )
        DemoDataService._findings = findings
        DemoDataService._cases = cases

    def get_findings(self, limit: int = 10000) -> List[Dict]:
        return DemoDataService._findings[:limit]

    def get_finding(self, finding_id: str) -> Optional[Dict]:
        for f in DemoDataService._findings:
            if f.get("finding_id") == finding_id:
                return f
        return None

    def get_cases(self, limit: int = 10000) -> List[Dict]:
        return DemoDataService._cases[:limit]

    def get_case(self, case_id: str) -> Optional[Dict]:
        for c in DemoDataService._cases:
            if c.get("case_id") == case_id:
                return c
        return None

    def create_finding(self, finding_data: Dict) -> Optional[Dict]:
        finding_data["demo"] = True
        DemoDataService._findings.append(finding_data)
        return finding_data

    def create_case(
        self,
        title: str,
        finding_ids: List[str],
        priority: str = "medium",
        description: str = "",
        status: str = "open",
        case_id: Optional[str] = None,
    ) -> Optional[Dict]:
        now = datetime.now(timezone.utc)
        case = {
            "case_id": case_id
            or f"case-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
            "title": title,
            "description": description,
            "finding_ids": finding_ids,
            "status": status,
            "priority": priority,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "tags": ["demo"],
            "demo": True,
            "notes": [],
            "timeline": [{"timestamp": now.isoformat(), "event": "Case created"}],
            "activities": [],
            "resolution_steps": [],
        }
        DemoDataService._cases.append(case)
        return case

    def update_finding(self, finding_id: str, **updates) -> bool:
        for f in DemoDataService._findings:
            if f.get("finding_id") == finding_id:
                f.update(updates)
                return True
        return False

    def update_case(self, case_id: str, **updates) -> bool:
        for c in DemoDataService._cases:
            if c.get("case_id") == case_id:
                c.update(updates)
                c["updated_at"] = datetime.now(timezone.utc).isoformat()
                return True
        return False

    def add_finding_to_case(self, case_id: str, finding_id: str) -> bool:
        """Add a finding to an existing case."""
        for c in DemoDataService._cases:
            if c.get("case_id") == case_id:
                if "finding_ids" not in c:
                    c["finding_ids"] = []
                if finding_id not in c["finding_ids"]:
                    c["finding_ids"].append(finding_id)
                    c["updated_at"] = datetime.now(timezone.utc).isoformat()
                return True
        return False

    def delete_case(self, case_id: str) -> bool:
        for i, c in enumerate(DemoDataService._cases):
            if c.get("case_id") == case_id:
                DemoDataService._cases.pop(i)
                return True
        return False

    def reset(self):
        """Restore the sample estate, dropping demo edits and re-timing it to now."""
        DemoDataService._initialized = False
        self._generate_demo_data()
        DemoDataService._initialized = True
