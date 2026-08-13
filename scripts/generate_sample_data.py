#!/usr/bin/env python3
"""
Sample Data Generator for Vigil SOC

Generates realistic sample findings and cases for testing.

Usage:
    python scripts/generate_sample_data.py              # Generate 20 findings + 3 cases
    python scripts/generate_sample_data.py --count 50   # Generate 50 findings
    python scripts/generate_sample_data.py --api        # Ingest via API
    python scripts/generate_sample_data.py --file       # Save to JSON files
"""

import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.platform.demo_data_service import (  # noqa: E402  (needs sys.path above)
    CLUSTERS,
    DATA_SOURCES,
    MITRE_TECHNIQUES,
    SEVERITIES,
    SEVERITY_WEIGHTS,
    generate_embedding,
    generate_entity_context,
    generate_mitre_predictions,
    generate_username,
)


def generate_finding(days_back: int = 30) -> Dict[str, Any]:
    """Generate a single sample finding."""
    timestamp = datetime.now(timezone.utc) - timedelta(
        days=random.randint(0, days_back),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
    )

    severity = random.choices(SEVERITIES, weights=SEVERITY_WEIGHTS)[0]

    finding = {
        "finding_id": f"f-{timestamp.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
        "timestamp": timestamp.isoformat(),
        "data_source": random.choice(DATA_SOURCES),
        "anomaly_score": round(random.uniform(0.3, 0.99), 3),
        "severity": severity,
        "status": (
            random.choice(["new", "investigating", "resolved"])
            if random.random() > 0.3
            else "new"
        ),
        "mitre_predictions": generate_mitre_predictions(),
        "entity_context": generate_entity_context(),
        "cluster_id": random.choice(CLUSTERS) if random.random() < 0.7 else None,
        "embedding": generate_embedding(),
    }

    # Add description based on detected techniques
    techniques = list(finding["mitre_predictions"].keys())
    if techniques:
        primary_technique = techniques[0]
        technique_name = MITRE_TECHNIQUES.get(primary_technique, "Unknown Technique")
        finding["description"] = (
            f"Detected {technique_name} ({primary_technique}) activity from {finding['data_source']} data source"
        )
    else:
        finding["description"] = (
            f"Anomalous activity detected from {finding['data_source']}"
        )

    return finding


def generate_case(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate a case from related findings."""
    now = datetime.now(timezone.utc)

    # Select 2-5 related findings
    num_findings = min(random.randint(2, 5), len(findings))
    selected_findings = random.sample(findings, num_findings)
    finding_ids = [f["finding_id"] for f in selected_findings]

    # Determine case priority based on finding severities
    severities = [f["severity"] for f in selected_findings]
    if "critical" in severities:
        priority = "critical"
    elif "high" in severities:
        priority = "high"
    else:
        priority = "medium"

    # Get cluster for title
    clusters = [f.get("cluster_id") for f in selected_findings if f.get("cluster_id")]
    title_cluster = clusters[0] if clusters else "suspicious-activity"

    case = {
        "case_id": f"case-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
        "title": f"Investigation: {title_cluster.replace('c-', '').replace('-', ' ').title()}",
        "description": f"Case created from {num_findings} correlated findings",
        "finding_ids": finding_ids,
        "status": random.choice(["open", "investigating", "closed"]),
        "priority": priority,
        "assignee": (
            generate_username() + "@company.com" if random.random() < 0.5 else ""
        ),
        "tags": [
            "auto-generated",
            random.choice(["malware", "lateral-movement", "exfiltration", "phishing"]),
        ],
        "notes": [],
        "timeline": [
            {
                "timestamp": now.isoformat(),
                "event": "Case created by sample data generator",
            }
        ],
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }

    return case


def generate_sample_data(num_findings: int = 20, num_cases: int = 3) -> tuple:
    """Generate sample findings and cases."""
    print(f"Generating {num_findings} sample findings...")
    findings = [generate_finding() for _ in range(num_findings)]

    print(f"Generating {num_cases} sample cases...")
    cases = [generate_case(findings) for _ in range(num_cases)]

    return findings, cases


def save_to_files(findings: List[Dict], cases: List[Dict]):
    """Save sample data to JSON files."""
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(exist_ok=True)

    # Save findings
    findings_file = data_dir / "findings.json"
    with open(findings_file, "w") as f:
        json.dump({"findings": findings}, f, indent=2)
    print(f"✓ Saved {len(findings)} findings to {findings_file}")

    # Save cases
    cases_file = data_dir / "cases.json"
    with open(cases_file, "w") as f:
        json.dump({"cases": cases}, f, indent=2)
    print(f"✓ Saved {len(cases)} cases to {cases_file}")


def ingest_via_api(
    findings: List[Dict], cases: List[Dict], base_url: str = "http://127.0.0.1:6987"
):
    """Ingest sample data via the API."""
    import requests

    print(f"\nIngesting data via API at {base_url}...")

    # Ingest findings
    print(f"Ingesting {len(findings)} findings...")
    findings_success = 0
    for finding in findings:
        try:
            # Remove embedding for API ingestion (too large for form data)
            finding_data = {k: v for k, v in finding.items() if k != "embedding"}
            finding_data["embedding"] = [0.0] * 768  # Placeholder

            response = requests.post(
                f"{base_url}/api/ingest/ingest-string",
                data={
                    "data": json.dumps(finding_data),
                    "format": "json",
                    "data_type": "finding",
                },
            )
            if response.status_code == 200:
                findings_success += 1
            else:
                print(f"  ⚠ Failed to ingest finding: {response.text}")
        except Exception as e:
            print(f"  ⚠ Error: {e}")

    print(f"✓ Ingested {findings_success}/{len(findings)} findings")

    # Ingest cases
    print(f"Ingesting {len(cases)} cases...")
    cases_success = 0
    for case in cases:
        try:
            response = requests.post(
                f"{base_url}/api/ingest/ingest-string",
                data={"data": json.dumps(case), "format": "json", "data_type": "case"},
            )
            if response.status_code == 200:
                cases_success += 1
            else:
                print(f"  ⚠ Failed to ingest case: {response.text}")
        except Exception as e:
            print(f"  ⚠ Error: {e}")

    print(f"✓ Ingested {cases_success}/{len(cases)} cases")


def print_sample(findings: List[Dict], cases: List[Dict]):
    """Print sample data for inspection."""
    print("\n" + "=" * 60)
    print(" Sample Finding:")
    print("=" * 60)
    sample = findings[0]
    print(f"  ID: {sample['finding_id']}")
    print(f"  Timestamp: {sample['timestamp']}")
    print(f"  Data Source: {sample['data_source']}")
    print(f"  Severity: {sample['severity']}")
    print(f"  Anomaly Score: {sample['anomaly_score']}")
    print(f"  MITRE Predictions: {sample['mitre_predictions']}")
    print(f"  Entity Context: {sample['entity_context']}")
    print(f"  Cluster: {sample.get('cluster_id', 'None')}")
    print(f"  Description: {sample['description']}")

    if cases:
        print("\n" + "=" * 60)
        print(" Sample Case:")
        print("=" * 60)
        case = cases[0]
        print(f"  ID: {case['case_id']}")
        print(f"  Title: {case['title']}")
        print(f"  Priority: {case['priority']}")
        print(f"  Status: {case['status']}")
        print(f"  Findings: {len(case['finding_ids'])}")
        print(f"  Tags: {case['tags']}")


def main():
    parser = argparse.ArgumentParser(description="Generate sample data for Vigil SOC")
    parser.add_argument(
        "--count", "-n", type=int, default=20, help="Number of findings to generate"
    )
    parser.add_argument(
        "--cases", "-c", type=int, default=3, help="Number of cases to generate"
    )
    parser.add_argument("--api", action="store_true", help="Ingest via API")
    parser.add_argument("--file", action="store_true", help="Save to JSON files")
    parser.add_argument("--url", default="http://127.0.0.1:6987", help="API base URL")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print(" Vigil SOC - Sample Data Generator")
    print("=" * 60)

    # Generate data
    findings, cases = generate_sample_data(args.count, args.cases)

    # Show sample
    print_sample(findings, cases)

    # Summary
    print("\n" + "=" * 60)
    print(" Summary:")
    print("=" * 60)
    print(f"  Total Findings: {len(findings)}")
    print(f"  Total Cases: {len(cases)}")

    # Save or ingest
    if args.file or not args.api:
        save_to_files(findings, cases)

    if args.api:
        ingest_via_api(findings, cases, args.url)

    print("\n✓ Done!\n")


if __name__ == "__main__":
    main()
