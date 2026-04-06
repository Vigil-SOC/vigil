#!/usr/bin/env python3
"""
Generate comprehensive test data for Splunk to test Claude integration.

This script generates realistic security events and sends them to Splunk
via the HTTP Event Collector (HEC) or outputs them as JSON for manual import.
"""

import os
import sys
import json
import random
import argparse
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
import requests
import urllib3

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SplunkTestDataGenerator:
    """Generate realistic security test data for Splunk."""
    
    def __init__(self):
        """Initialize test data generator."""
        # Attacker IPs (external)
        self.attacker_ips = [
            "185.220.101.45",
            "45.142.214.123",
            "91.203.5.165",
            "179.43.187.90",
            "103.253.145.78",
            "194.180.48.221",
            "23.129.64.216",
            "103.77.192.82",
        ]
        
        # Internal victim IPs
        self.victim_ips = [
            "10.0.1.15",
            "10.0.1.23",
            "10.0.1.45",
            "10.0.2.10",
            "10.0.2.18",
            "192.168.1.100",
            "192.168.1.150",
            "172.16.0.50",
        ]
        
        # Malicious domains
        self.malicious_domains = [
            "evil-command-control.com",
            "malware-download.net",
            "phishing-site.org",
            "crypto-miner-pool.io",
            "ransomware-c2.xyz",
            "data-exfil-server.ru",
            "backdoor-server.cn",
            "exploit-kit.tk",
        ]
        
        # Benign domains for noise
        self.benign_domains = [
            "microsoft.com",
            "google.com",
            "cloudflare.com",
            "amazon.com",
            "github.com",
            "stackoverflow.com",
        ]
        
        # Malware hashes
        self.malware_hashes = {
            "md5": [
                "44d88612fea8a8f36de82e1278abb02f",
                "5d41402abc4b2a76b9719d911017c592",
                "098f6bcd4621d373cade4e832627b4f6",
                "e4d909c290d0fb1ca068ffaddf22cbd0",
            ],
            "sha256": [
                "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
                "6ca13d52ca70c883e0f0bb101e425a89e8624de51db2d2392593af6a84118090",
                "cf80cd8aed482d5d1527d7dc72fceff84e6326592848447d2dc0b0e87dfc9a90",
            ],
        }
        
        # Usernames
        self.usernames = [
            "jsmith",
            "adavis",
            "mjohnson",
            "bwilliams",
            "admin",
            "service_account",
            "backup_user",
        ]
        
        # Hostnames
        self.hostnames = [
            "DESKTOP-WKS001",
            "LAPTOP-USER02",
            "SERVER-DC01",
            "SERVER-WEB01",
            "SERVER-DB01",
            "WORKSTATION-10",
        ]
        
        # MITRE ATT&CK mappings
        self.mitre_tactics = {
            "Initial Access": ["T1566.001", "T1190", "T1133"],
            "Execution": ["T1059.001", "T1059.003", "T1053.005"],
            "Persistence": ["T1543.003", "T1547.001", "T1136.001"],
            "Privilege Escalation": ["T1548.002", "T1134", "T1068"],
            "Defense Evasion": ["T1562.001", "T1070.004", "T1027"],
            "Credential Access": ["T1110.003", "T1003.001", "T1555"],
            "Discovery": ["T1083", "T1046", "T1087.001"],
            "Lateral Movement": ["T1021.001", "T1021.002", "T1563.002"],
            "Collection": ["T1005", "T1039", "T1114.001"],
            "Command and Control": ["T1071.001", "T1573", "T1095"],
            "Exfiltration": ["T1041", "T1048.003", "T1567.002"],
            "Impact": ["T1486", "T1490", "T1498"],
        }
    
    def generate_brute_force_events(self, count: int = 50) -> List[Dict[str, Any]]:
        """Generate brute force attack events."""
        events = []
        base_time = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 24))
        
        attacker = random.choice(self.attacker_ips)
        victim = random.choice(self.victim_ips)
        username = random.choice(self.usernames)
        
        for i in range(count):
            event_time = base_time + timedelta(seconds=i * random.randint(1, 5))
            
            events.append({
                "_time": event_time.isoformat(),
                "sourcetype": "WinEventLog:Security",
                "source": "XmlWinEventLog:Security",
                "host": random.choice(self.hostnames),
                "EventCode": "4625",
                "event_id": "4625",
                "signature": "An account failed to log on",
                "severity": "high" if i > 40 else "medium",
                "urgency": "high" if i > 40 else "medium",
                "src": attacker,
                "src_ip": attacker,
                "dest": victim,
                "dest_ip": victim,
                "user": username,
                "username": username,
                "action": "failure",
                "result": "failed",
                "rule_name": "Multiple Failed Logon Attempts",
                "rule_title": "Brute Force Attack Detected",
                "rule_description": f"Multiple failed logon attempts detected from {attacker}",
                "category": "Authentication",
                "subcategory": "Brute Force",
                "mitre_tactic": "Credential Access",
                "mitre_technique": "T1110.003",
                "priority": "high" if i > 40 else "medium",
                "count": i + 1,
                "failure_reason": random.choice([
                    "Unknown user name or bad password",
                    "Account disabled",
                    "Account locked out",
                ]),
                "_raw": f"{event_time.isoformat()} EventCode=4625 src={attacker} dest={victim} user={username} action=failure",
            })
        
        logger.info(f"Generated {len(events)} brute force attack events")
        return events
    
    def generate_malware_events(self, count: int = 30) -> List[Dict[str, Any]]:
        """Generate malware detection events."""
        events = []
        base_time = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 48))
        
        for i in range(count):
            event_time = base_time + timedelta(minutes=i * random.randint(5, 30))
            victim = random.choice(self.victim_ips)
            hostname = random.choice(self.hostnames)
            md5_hash = random.choice(self.malware_hashes["md5"])
            sha256_hash = random.choice(self.malware_hashes["sha256"])
            
            malware_names = [
                "Trojan.GenericKD.12345678",
                "Ransomware.WannaCry",
                "Backdoor.RemoteAdmin",
                "Trojan.Emotet",
                "Malware.Generic",
            ]
            
            events.append({
                "_time": event_time.isoformat(),
                "sourcetype": "av:alert",
                "source": "antivirus",
                "host": hostname,
                "signature": random.choice(malware_names),
                "signature_id": f"MAL-{random.randint(10000, 99999)}",
                "severity": random.choice(["high", "critical"]),
                "urgency": "high",
                "dest": victim,
                "dest_ip": victim,
                "file_path": random.choice([
                    "C:\\Users\\Public\\Downloads\\malware.exe",
                    "C:\\Windows\\Temp\\suspicious.dll",
                    "C:\\ProgramData\\evil\\backdoor.exe",
                ]),
                "file_hash_md5": md5_hash,
                "file_hash_sha256": sha256_hash,
                "action": "quarantined",
                "rule_name": "Malware Detected",
                "rule_title": "Malicious File Detection",
                "rule_description": "Antivirus detected and quarantined malicious file",
                "category": "Malware",
                "subcategory": "Endpoint",
                "mitre_tactic": "Execution",
                "mitre_technique": "T1059.001",
                "priority": "critical",
                "_raw": f"{event_time.isoformat()} Malware detected: {malware_names[0]} on {hostname}",
            })
        
        logger.info(f"Generated {len(events)} malware detection events")
        return events
    
    def generate_c2_traffic_events(self, count: int = 100) -> List[Dict[str, Any]]:
        """Generate command and control traffic events."""
        events = []
        base_time = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 72))
        
        victim = random.choice(self.victim_ips)
        c2_domain = random.choice(self.malicious_domains)
        c2_ip = random.choice(self.attacker_ips)
        
        for i in range(count):
            event_time = base_time + timedelta(minutes=i * random.randint(1, 15))
            
            events.append({
                "_time": event_time.isoformat(),
                "sourcetype": "firewall",
                "source": "paloalto:firewall",
                "host": "firewall-01",
                "signature": "Suspicious Outbound Traffic",
                "signature_id": "C2-BEACON",
                "severity": "high",
                "urgency": "high",
                "src": victim,
                "src_ip": victim,
                "dest": c2_ip,
                "dest_ip": c2_ip,
                "dest_domain": c2_domain,
                "dest_port": random.choice([443, 8080, 4443]),
                "protocol": "tcp",
                "action": random.choice(["allowed", "blocked"]),
                "bytes_out": random.randint(1024, 102400),
                "bytes_in": random.randint(512, 10240),
                "rule_name": "Command and Control Traffic",
                "rule_title": "C2 Beacon Detected",
                "rule_description": f"Suspicious periodic traffic to known C2 domain {c2_domain}",
                "category": "Network",
                "subcategory": "Command and Control",
                "mitre_tactic": "Command and Control",
                "mitre_technique": "T1071.001",
                "priority": "high",
                "threat_intel_match": "known_c2_domain",
                "_raw": f"{event_time.isoformat()} src={victim} dest={c2_ip} domain={c2_domain} action=allowed",
            })
        
        logger.info(f"Generated {len(events)} C2 traffic events")
        return events
    
    def generate_data_exfiltration_events(self, count: int = 20) -> List[Dict[str, Any]]:
        """Generate data exfiltration events."""
        events = []
        base_time = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 12))
        
        insider = random.choice(self.usernames)
        victim_host = random.choice(self.hostnames)
        exfil_domain = random.choice(self.malicious_domains)
        
        for i in range(count):
            event_time = base_time + timedelta(minutes=i * random.randint(5, 20))
            
            events.append({
                "_time": event_time.isoformat(),
                "sourcetype": "dlp:alert",
                "source": "data_loss_prevention",
                "host": victim_host,
                "signature": "Large Data Transfer",
                "signature_id": "DLP-001",
                "severity": "critical",
                "urgency": "critical",
                "src": random.choice(self.victim_ips),
                "dest_domain": exfil_domain,
                "user": insider,
                "username": insider,
                "bytes_out": random.randint(1048576, 104857600),  # 1MB to 100MB
                "file_count": random.randint(10, 500),
                "action": "logged",
                "rule_name": "Data Exfiltration Detected",
                "rule_title": "Unusual Large Data Transfer",
                "rule_description": f"User {insider} transferred large amounts of data to external domain",
                "category": "Data Loss",
                "subcategory": "Exfiltration",
                "mitre_tactic": "Exfiltration",
                "mitre_technique": "T1041",
                "priority": "critical",
                "data_classification": "confidential",
                "_raw": f"{event_time.isoformat()} User {insider} uploaded {random.randint(10, 500)} files to {exfil_domain}",
            })
        
        logger.info(f"Generated {len(events)} data exfiltration events")
        return events
    
    def generate_privilege_escalation_events(self, count: int = 15) -> List[Dict[str, Any]]:
        """Generate privilege escalation events."""
        events = []
        base_time = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 24))
        
        attacker_user = random.choice(self.usernames)
        victim_host = random.choice(self.hostnames)
        
        for i in range(count):
            event_time = base_time + timedelta(minutes=i * random.randint(3, 10))
            
            events.append({
                "_time": event_time.isoformat(),
                "sourcetype": "WinEventLog:Security",
                "source": "XmlWinEventLog:Security",
                "host": victim_host,
                "EventCode": random.choice(["4672", "4728", "4732"]),
                "signature": "Privilege Escalation Attempt",
                "severity": "high",
                "urgency": "high",
                "user": attacker_user,
                "username": attacker_user,
                "target_user": "Administrator",
                "group": "Domain Admins",
                "action": random.choice(["success", "failure"]),
                "rule_name": "Privilege Escalation",
                "rule_title": "User Added to Privileged Group",
                "rule_description": f"User {attacker_user} attempted to escalate privileges",
                "category": "Privilege Escalation",
                "subcategory": "Account Manipulation",
                "mitre_tactic": "Privilege Escalation",
                "mitre_technique": "T1134",
                "priority": "high",
                "_raw": f"{event_time.isoformat()} EventCode=4728 user={attacker_user} group='Domain Admins'",
            })
        
        logger.info(f"Generated {len(events)} privilege escalation events")
        return events
    
    def generate_lateral_movement_events(self, count: int = 25) -> List[Dict[str, Any]]:
        """Generate lateral movement events."""
        events = []
        base_time = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 36))
        
        attacker = random.choice(self.usernames)
        source_host = random.choice(self.hostnames)
        
        for i in range(count):
            event_time = base_time + timedelta(minutes=i * random.randint(5, 15))
            dest_host = random.choice([h for h in self.hostnames if h != source_host])
            
            events.append({
                "_time": event_time.isoformat(),
                "sourcetype": "WinEventLog:Security",
                "source": "XmlWinEventLog:Security",
                "host": dest_host,
                "EventCode": "4624",
                "signature": "Successful Logon",
                "severity": "medium",
                "urgency": "medium",
                "src": random.choice(self.victim_ips),
                "dest": random.choice(self.victim_ips),
                "user": attacker,
                "username": attacker,
                "logon_type": random.choice(["3", "10"]),  # Network or RemoteInteractive
                "action": "success",
                "rule_name": "Lateral Movement",
                "rule_title": "Suspicious Network Logon",
                "rule_description": f"User {attacker} logged on from {source_host} to {dest_host}",
                "category": "Lateral Movement",
                "subcategory": "Remote Services",
                "mitre_tactic": "Lateral Movement",
                "mitre_technique": "T1021.001",
                "priority": "medium",
                "protocol": random.choice(["RDP", "SMB", "WinRM"]),
                "_raw": f"{event_time.isoformat()} EventCode=4624 user={attacker} logon_type=3 dest={dest_host}",
            })
        
        logger.info(f"Generated {len(events)} lateral movement events")
        return events
    
    def generate_reconnaissance_events(self, count: int = 40) -> List[Dict[str, Any]]:
        """Generate network reconnaissance events."""
        events = []
        base_time = datetime.now(timezone.utc) - timedelta(hours=random.randint(1, 48))
        
        attacker = random.choice(self.attacker_ips)
        
        for i in range(count):
            event_time = base_time + timedelta(seconds=i * random.randint(1, 10))
            
            events.append({
                "_time": event_time.isoformat(),
                "sourcetype": "ids",
                "source": "suricata",
                "host": "ids-sensor-01",
                "signature": random.choice([
                    "Network Port Scanner",
                    "Service Enumeration",
                    "OS Fingerprinting",
                ]),
                "signature_id": f"RECON-{random.randint(1000, 9999)}",
                "severity": "medium",
                "urgency": "medium",
                "src": attacker,
                "src_ip": attacker,
                "dest": random.choice(self.victim_ips),
                "dest_port": random.randint(1, 65535),
                "protocol": "tcp",
                "action": "alerted",
                "rule_name": "Network Reconnaissance",
                "rule_title": "Port Scanning Activity",
                "rule_description": f"Host {attacker} performing network reconnaissance",
                "category": "Reconnaissance",
                "subcategory": "Network Scanning",
                "mitre_tactic": "Discovery",
                "mitre_technique": "T1046",
                "priority": "medium",
                "_raw": f"{event_time.isoformat()} Port scan from {attacker}",
            })
        
        logger.info(f"Generated {len(events)} reconnaissance events")
        return events
    
    def generate_all_test_data(self) -> List[Dict[str, Any]]:
        """Generate all types of test data."""
        all_events = []
        
        all_events.extend(self.generate_brute_force_events(50))
        all_events.extend(self.generate_malware_events(30))
        all_events.extend(self.generate_c2_traffic_events(100))
        all_events.extend(self.generate_data_exfiltration_events(20))
        all_events.extend(self.generate_privilege_escalation_events(15))
        all_events.extend(self.generate_lateral_movement_events(25))
        all_events.extend(self.generate_reconnaissance_events(40))
        
        # Sort by time
        all_events.sort(key=lambda x: x["_time"])
        
        logger.info(f"Generated {len(all_events)} total events")
        return all_events
    
    def save_to_file(self, events: List[Dict[str, Any]], filename: str = "splunk_test_data.json"):
        """Save events to JSON file."""
        with open(filename, 'w') as f:
            json.dump(events, f, indent=2)
        logger.info(f"Saved {len(events)} events to {filename}")
    
    def send_to_splunk_hec(self, events: List[Dict[str, Any]], 
                          hec_url: str, hec_token: str, 
                          index: str = "main", 
                          verify_ssl: bool = False):
        """
        Send events to Splunk HTTP Event Collector.
        
        Args:
            events: List of events to send
            hec_url: Splunk HEC URL (e.g., https://splunk:8088/services/collector)
            hec_token: HEC token for authentication
            index: Target Splunk index
            verify_ssl: Whether to verify SSL certificates
        """
        headers = {
            "Authorization": f"Splunk {hec_token}",
            "Content-Type": "application/json"
        }
        
        success_count = 0
        error_count = 0
        
        # Send events in batches of 100
        batch_size = 100
        for i in range(0, len(events), batch_size):
            batch = events[i:i + batch_size]
            
            # Format for HEC (multiple events)
            hec_payload = ""
            reserved_keys = {"_time", "host", "source", "sourcetype", "index"}
            for event in batch:
                # Convert ISO timestamp to Unix epoch float required by HEC
                iso_time = event.get("_time")
                try:
                    dt = datetime.fromisoformat(iso_time)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    unix_time = dt.timestamp()
                except (ValueError, TypeError):
                    logger.warning(f"Failed to parse _time '{iso_time}', using current UTC time")
                    unix_time = datetime.now(timezone.utc).timestamp()

                # Strip reserved Splunk fields from the inner event object
                clean_event = {k: v for k, v in event.items() if k not in reserved_keys}

                hec_event = {
                    "time": unix_time,
                    "sourcetype": event.get("sourcetype", "json"),
                    "source": event.get("source", "test_data_generator"),
                    "host": event.get("host", "test-host"),
                    "index": index,
                    "event": clean_event
                }
                hec_payload += json.dumps(hec_event) + "\n"
            
            try:
                response = requests.post(
                    hec_url,
                    headers=headers,
                    data=hec_payload.encode('utf-8'),
                    verify=verify_ssl,
                    timeout=30
                )
                
                if response.status_code == 200:
                    success_count += len(batch)
                    logger.info(f"Sent batch {i // batch_size + 1} ({len(batch)} events) successfully")
                else:
                    error_count += len(batch)
                    logger.error(f"Failed to send batch: {response.status_code} - {response.text}")
            
            except Exception as e:
                error_count += len(batch)
                logger.error(f"Error sending batch: {e}")
        
        logger.info(f"Completed: {success_count} events sent successfully, {error_count} errors")
        return success_count, error_count


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Generate Splunk test data")
    parser.add_argument(
        "--output",
        "-o",
        default="splunk_test_data.json",
        help="Output JSON file (default: splunk_test_data.json)"
    )
    parser.add_argument(
        "--send-to-splunk",
        action="store_true",
        help="Send data directly to Splunk HEC"
    )
    parser.add_argument(
        "--hec-url",
        help="Splunk HEC URL (e.g., https://splunk:8088/services/collector)"
    )
    parser.add_argument(
        "--hec-token",
        help="Splunk HEC token"
    )
    parser.add_argument(
        "--index",
        default="main",
        help="Splunk index (default: main)"
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable SSL verification"
    )
    
    args = parser.parse_args()
    
    # Generate data
    generator = SplunkTestDataGenerator()
    events = generator.generate_all_test_data()
    
    # Save to file
    generator.save_to_file(events, args.output)
    
    # Send to Splunk if requested
    if args.send_to_splunk:
        if not args.hec_url or not args.hec_token:
            logger.error("--hec-url and --hec-token are required when using --send-to-splunk")
            sys.exit(1)
        
        generator.send_to_splunk_hec(
            events,
            args.hec_url,
            args.hec_token,
            args.index,
            verify_ssl=not args.no_verify_ssl
        )
    
    logger.info(f"""
Test data generation complete!

Summary:
- Total events: {len(events)}
- Output file: {args.output}

Event types:
- Brute force attacks: 50 events
- Malware detections: 30 events
- C2 traffic: 100 events
- Data exfiltration: 20 events
- Privilege escalation: 15 events
- Lateral movement: 25 events
- Reconnaissance: 40 events

To import into Splunk manually:
1. Go to Settings > Add Data > Upload
2. Select {args.output}
3. Set sourcetype to 'json'
4. Choose your index
5. Review and submit

To send directly to Splunk HEC:
python {sys.argv[0]} --send-to-splunk \\
    --hec-url https://your-splunk:8088/services/collector \\
    --hec-token your-hec-token \\
    --index main \\
    --no-verify-ssl
""")


if __name__ == "__main__":
    main()

