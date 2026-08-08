"""Demo data service for Vigil SOC demo mode."""

import random
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

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
    "c-beaconing-001", "c-lateral-movement-002", "c-exfiltration-003",
    "c-credential-theft-004", "c-ransomware-005", "c-phishing-006",
    "c-c2-communication-007", "c-persistence-008",
]
SEVERITIES = ["critical", "high", "medium", "low"]
SEVERITY_WEIGHTS = [0.1, 0.25, 0.4, 0.25]


class DemoDataService:
    """Service that provides demo/sample data for demo mode."""
    
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
    
    def _generate_embedding(self, dim: int = 768) -> List[float]:
        vec = [random.gauss(0, 1) for _ in range(dim)]
        norm = sum(x**2 for x in vec) ** 0.5
        return [x / norm for x in vec]
    
    def _generate_ip(self) -> str:
        if random.random() < 0.6:
            return f"192.168.{random.randint(1, 254)}.{random.randint(1, 254)}"
        return f"{random.randint(1, 223)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"
    
    def _generate_hostname(self) -> str:
        prefixes = ["ws", "srv", "dc", "web", "db", "app", "dev", "prod"]
        return f"{random.choice(prefixes)}-{random.randint(1, 999):03d}"
    
    def _generate_username(self) -> str:
        names = ["john", "jane", "bob", "alice", "admin", "service", "backup"]
        return f"{random.choice(names)}{random.randint(1, 99)}"
    
    def _generate_mitre_predictions(self) -> Dict[str, float]:
        num_techniques = random.randint(1, 4)
        techniques = random.sample(list(MITRE_TECHNIQUES.keys()), num_techniques)
        return {tech: round(random.uniform(0.5, 0.99), 3) for tech in techniques}
    
    def _generate_entity_context(self) -> Dict[str, Any]:
        context = {}
        if random.random() < 0.8:
            context["src_ips"] = [self._generate_ip() for _ in range(random.randint(1, 3))]
        if random.random() < 0.6:
            context["dest_ips"] = [self._generate_ip() for _ in range(random.randint(1, 2))]
        if random.random() < 0.7:
            context["hostnames"] = [self._generate_hostname() for _ in range(random.randint(1, 2))]
        if random.random() < 0.5:
            context["usernames"] = [self._generate_username()]
        if random.random() < 0.3:
            context["file_hashes"] = [uuid.uuid4().hex for _ in range(random.randint(1, 2))]
        return context
    
    def _generate_finding(self, days_back: int = 30) -> Dict[str, Any]:
        timestamp = datetime.now(timezone.utc) - timedelta(
            days=random.randint(0, days_back),
            hours=random.randint(0, 23),
            minutes=random.randint(0, 59)
        )
        severity = random.choices(SEVERITIES, weights=SEVERITY_WEIGHTS)[0]
        mitre_preds = self._generate_mitre_predictions()
        
        techniques = list(mitre_preds.keys())
        if techniques:
            primary = techniques[0]
            desc = f"Detected {MITRE_TECHNIQUES.get(primary, 'Unknown')} ({primary}) activity"
        else:
            desc = "Anomalous activity detected"
        
        return {
            "finding_id": f"f-{timestamp.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
            "timestamp": timestamp.isoformat(),
            "data_source": random.choice(DATA_SOURCES),
            "anomaly_score": round(random.uniform(0.3, 0.99), 3),
            "severity": severity,
            "status": random.choice(["new", "investigating", "resolved"]) if random.random() > 0.3 else "new",
            "mitre_predictions": mitre_preds,
            "predicted_techniques": [
                {"technique_id": t, "confidence": c, "technique_name": MITRE_TECHNIQUES.get(t, "")}
                for t, c in mitre_preds.items()
            ],
            "entity_context": self._generate_entity_context(),
            "cluster_id": random.choice(CLUSTERS) if random.random() < 0.7 else None,
            "embedding": self._generate_embedding(),
            "description": desc,
        }
    
    def _generate_case(self, findings: List[Dict]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        num_findings = min(random.randint(2, 5), len(findings))
        selected = random.sample(findings, num_findings)
        finding_ids = [f["finding_id"] for f in selected]
        
        severities = [f["severity"] for f in selected]
        if "critical" in severities:
            priority = "critical"
        elif "high" in severities:
            priority = "high"
        else:
            priority = "medium"
        
        clusters = [f.get("cluster_id") for f in selected if f.get("cluster_id")]
        title_cluster = clusters[0] if clusters else "suspicious-activity"
        
        return {
            "case_id": f"case-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
            "title": f"Investigation: {title_cluster.replace('c-', '').replace('-', ' ').title()}",
            "description": f"Demo case with {num_findings} correlated findings",
            "finding_ids": finding_ids,
            "status": random.choice(["open", "investigating", "closed"]),
            "priority": priority,
            "assignee": self._generate_username() + "@company.com" if random.random() < 0.5 else "",
            "tags": ["demo", random.choice(["malware", "lateral-movement", "exfiltration", "phishing"])],
            "notes": [],
            "timeline": [{"timestamp": now.isoformat(), "event": "Case created (demo mode)"}],
            "activities": [],
            "resolution_steps": [],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    
    def _generate_demo_data(self, num_findings: int = 25, num_cases: int = 5):
        """Generate demo findings and cases."""
        logger.info(f"Generating demo data: {num_findings} findings, {num_cases} cases")
        random.seed(42)  # Consistent demo data
        DemoDataService._findings = [self._generate_finding() for _ in range(num_findings)]
        DemoDataService._cases = [self._generate_case(DemoDataService._findings) for _ in range(num_cases)]
        logger.info("Demo data generated successfully")
    
    def get_findings(self, limit: int = 10000) -> List[Dict]:
        return DemoDataService._findings[:limit]
    
    def get_finding(self, finding_id: str) -> Optional[Dict]:
        for f in DemoDataService._findings:
            if f.get("finding_id") == finding_id:
                return f
        return None
    
    def get_nearest_neighbors(self, finding_id: str, limit: int = 10) -> Dict:
        """Find similar findings using embedding-based cosine similarity.
        
        Args:
            finding_id: Reference finding ID to find neighbors for
            limit: Maximum number of neighbors to return
            
        Returns:
            Dict with seed_finding and neighbors list
        """
        seed = self.get_finding(finding_id)
        if not seed or 'embedding' not in seed:
            return {"error": f"Finding {finding_id} not found or has no embedding"}
        
        seed_emb = seed['embedding']
        sims = []
        for f in DemoDataService._findings:
            if f.get('finding_id') != finding_id and 'embedding' in f:
                # Cosine similarity
                a = f['embedding']
                dot = sum(x * y for x, y in zip(seed_emb, a))
                norm_a = sum(x ** 2 for x in seed_emb) ** 0.5
                norm_b = sum(x ** 2 for x in a) ** 0.5
                denom = norm_a * norm_b
                sim = dot / denom if denom > 0 else 0.0
                sims.append({
                    "finding_id": f['finding_id'],
                    "similarity": round(sim, 4),
                    "cluster_id": f.get('cluster_id'),
                    "severity": f.get('severity'),
                    "data_source": f.get('data_source'),
                    "anomaly_score": float(f.get('anomaly_score', 0)),
                })
        
        sims.sort(key=lambda x: x['similarity'], reverse=True)
        return {"seed_finding": finding_id, "neighbors": sims[:limit]}
    
    def get_cases(self, limit: int = 10000) -> List[Dict]:
        return DemoDataService._cases[:limit]
    
    def get_case(self, case_id: str) -> Optional[Dict]:
        for c in DemoDataService._cases:
            if c.get("case_id") == case_id:
                return c
        return None
    
    def create_finding(self, finding_data: Dict) -> Optional[Dict]:
        DemoDataService._findings.append(finding_data)
        return finding_data
    
    def create_case(self, title: str, finding_ids: List[str], priority: str = "medium",
                    description: str = "", status: str = "open") -> Optional[Dict]:
        now = datetime.now(timezone.utc)
        case = {
            "case_id": f"case-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
            "title": title,
            "description": description,
            "finding_ids": finding_ids,
            "status": status,
            "priority": priority,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "tags": ["demo"],
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
                if 'finding_ids' not in c:
                    c['finding_ids'] = []
                if finding_id not in c['finding_ids']:
                    c['finding_ids'].append(finding_id)
                    c['updated_at'] = datetime.now(timezone.utc).isoformat()
                return True
        return False
    
    def delete_case(self, case_id: str) -> bool:
        for i, c in enumerate(DemoDataService._cases):
            if c.get("case_id") == case_id:
                DemoDataService._cases.pop(i)
                return True
        return False
    
    def reset(self):
        """Regenerate demo data."""
        DemoDataService._initialized = False
        self._generate_demo_data()
        DemoDataService._initialized = True


_demo_service = None

def get_demo_service() -> DemoDataService:
    global _demo_service
    if _demo_service is None:
        _demo_service = DemoDataService()
    return _demo_service
