"""Attack data loader - converts findings to attack visualization data structure."""

from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any
from services.database_data_service import DatabaseDataService
from services.ingestion_service import MITRE_TACTIC_MAP
from services.mitre_lookup import TECHNIQUE_NAME_FALLBACKS

VALID_TACTICS = set(MITRE_TACTIC_MAP.values())


def findings_to_attack_data(findings: List[Dict]) -> Dict:
    """Convert LogLM findings to attack visualization data structure."""
    
    # Extract unique entities
    hosts = set()
    external_ips = set()
    users = set()
    domains = set()
    
    # Track connections
    connections = []
    timeline_events = []
    technique_counts = {}
    
    for finding in findings:
        entity = finding.get("entity_context", {})
        
        # Extract entities
        if entity.get("hostname"):
            hosts.add(entity["hostname"])
        if entity.get("src_ip", ""):
            if not (entity["src_ip"].startswith("10.") or 
                   entity["src_ip"].startswith("192.168.") or
                   entity["src_ip"].startswith("172.")):
                external_ips.add(entity["src_ip"])
        if entity.get("dst_ip"):
            if not (entity["dst_ip"].startswith("10.") or 
                   entity["dst_ip"].startswith("192.168.") or
                   entity["dst_ip"].startswith("172.")):
                external_ips.add(entity["dst_ip"])
        if entity.get("user"):
            users.add(entity["user"])
        if entity.get("query_name"):
            # Extract domain from DNS query
            parts = entity["query_name"].split(".")
            if len(parts) >= 2:
                domain = ".".join(parts[-2:])
                domains.add(domain)
        
        # Track MITRE techniques
        for technique, confidence in finding.get("mitre_predictions", {}).items():
            if technique not in technique_counts:
                technique_counts[technique] = {"count": 0, "total_confidence": 0}
            technique_counts[technique]["count"] += 1
            technique_counts[technique]["total_confidence"] += confidence
        
        # Create timeline event
        timestamp = finding.get("timestamp", datetime.now().isoformat())
        timeline_events.append({
            "time": timestamp[11:16] if len(timestamp) > 16 else "00:00",
            "event": f"{finding.get('data_source', 'unknown').upper()}: {entity.get('hostname', 'unknown')}",
            "severity": finding.get("severity", "medium"),
            "finding_id": finding.get("finding_id", ""),
            "anomaly_score": finding.get("anomaly_score", 0)
        })
    
    # Build nodes
    nodes = []
    
    for host in hosts:
        risk = "high" if "server" in host.lower() else "medium"
        role = "crown_jewel" if "server" in host.lower() else "endpoint"
        nodes.append({
            "id": host,
            "type": "server" if "server" in host.lower() else "endpoint",
            "label": host,
            "risk": risk,
            "role": role
        })
    
    for ip in list(external_ips)[:10]:  # Limit external IPs
        nodes.append({
            "id": ip,
            "type": "external",
            "label": f"{ip}\n(External)",
            "risk": "high",
            "role": "c2"
        })
    
    for domain in list(domains)[:5]:  # Limit domains
        nodes.append({
            "id": domain,
            "type": "domain",
            "label": f"*.{domain}",
            "risk": "medium",
            "role": "exfil"
        })
    
    # Build edges based on findings
    edges = []
    host_list = list(hosts)
    external_list = list(external_ips)
    domain_list = list(domains)
    
    # Connect hosts to external IPs (C2)
    if host_list and external_list:
        for host in host_list[:3]:
            for ext in external_list[:2]:
                edges.append({
                    "source": host,
                    "target": ext,
                    "type": "c2",
                    "label": "C2 Beacon"
                })
    
    # Connect hosts to domains (DNS)
    if host_list and domain_list:
        for host in host_list[:2]:
            for domain in domain_list[:2]:
                edges.append({
                    "source": host,
                    "target": domain,
                    "type": "dns",
                    "label": "DNS Query"
                })
    
    # Lateral movement between hosts
    if len(host_list) > 1:
        for i in range(min(len(host_list) - 1, 5)):
            edges.append({
                "source": host_list[i],
                "target": host_list[i + 1],
                "type": "lateral",
                "label": "Lateral Movement"
            })
    
    # Build MITRE techniques list
    mitre_techniques = []

    for technique, stats in technique_counts.items():
        if technique in TECHNIQUE_NAME_FALLBACKS:
            name, tactic = TECHNIQUE_NAME_FALLBACKS[technique]
        elif technique in VALID_TACTICS:
            name, tactic = technique, technique
        else:
            name, tactic = technique, "Unknown"
        mitre_techniques.append({
            "technique": technique,
            "name": name,
            "tactic": tactic,
            "confidence": round(stats["total_confidence"] / stats["count"], 3) if stats["count"] > 0 else 0,
            "count": stats["count"]
        })
    
    # Sort by count
    mitre_techniques.sort(key=lambda x: x["count"], reverse=True)
    
    # Determine phases based on techniques
    phases = []
    if any(t["tactic"] == "Initial Access" for t in mitre_techniques):
        phases.append({
            "phase": 1,
            "name": "Initial Access",
            "time": timeline_events[0]["time"] if timeline_events else "00:00",
            "description": "Initial compromise detected",
            "techniques": [t["technique"] for t in mitre_techniques if t["tactic"] == "Initial Access"],
            "entities": list(hosts)[:2]
        })
    
    if any(t["tactic"] == "Command and Control" for t in mitre_techniques):
        phases.append({
            "phase": 2,
            "name": "Command and Control",
            "time": "Ongoing",
            "description": "C2 infrastructure established",
            "techniques": [t["technique"] for t in mitre_techniques if t["tactic"] == "Command and Control"],
            "entities": list(external_ips)[:3]
        })
    
    if any(t["tactic"] == "Lateral Movement" for t in mitre_techniques):
        phases.append({
            "phase": 3,
            "name": "Lateral Movement",
            "time": "Ongoing",
            "description": "Network spread detected",
            "techniques": [t["technique"] for t in mitre_techniques if t["tactic"] == "Lateral Movement"],
            "entities": list(hosts)
        })
    
    if any(t["tactic"] == "Exfiltration" for t in mitre_techniques):
        phases.append({
            "phase": 4,
            "name": "Exfiltration",
            "time": "Ongoing",
            "description": "Data exfiltration detected",
            "techniques": [t["technique"] for t in mitre_techniques if t["tactic"] == "Exfiltration"],
            "entities": list(domains)[:3]
        })
    
    # Ensure at least some phases
    if not phases:
        phases = [
            {"phase": 1, "name": "Detection", "time": "00:00", "description": "Anomalies detected", 
             "techniques": [], "entities": list(hosts)[:3]}
        ]
    
    return {
        "attack_id": f"ATK-{datetime.now().strftime('%Y-%m-%d')}-001",
        "title": "LogLM Attack Flow Analysis",
        "severity": "HIGH" if any(f.get("severity") == "high" for f in findings) else "MEDIUM",
        "start_time": datetime.now().isoformat(),
        "end_time": datetime.now().isoformat(),
        "phases": phases,
        "nodes": nodes,
        "edges": edges,
        "timeline_events": timeline_events[:20],  # Limit to 20 events
        "mitre_techniques": mitre_techniques[:10]  # Limit to top 10
    }

