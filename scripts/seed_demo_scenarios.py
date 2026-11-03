#!/usr/bin/env python3
"""Seed the curated demo scenarios (Wazuh, LibreNMS, NetBox-verified devices) into a database.

The same findings and cases demo mode serves from memory
(``core/platform/demo_scenarios.py``), written through ``DatabaseService`` so
ATT&CK rows, case links and the case trail are created the normal way.

Dry run by default: it prints what it would write and touches nothing.
Re-running is safe; ids that already exist are skipped.

Usage:
    python scripts/seed_demo_scenarios.py            # dry run
    python scripts/seed_demo_scenarios.py --apply    # write to the configured database
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.platform.demo_scenarios import AUTO, build_scenarios  # noqa: E402


def _naive_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write to the database")
    args = parser.parse_args()

    findings, cases = build_scenarios(datetime.now(timezone.utc))
    routine = sum(1 for f in findings if f["scenario"] == AUTO)
    print(
        f"{len(findings)} findings ({routine} routine, {len(findings) - routine} "
        f"need a person) and {len(cases)} cases"
    )
    for f in findings:
        print(
            f"  {f['scenario']:<13} {f['data_source']:<9} {f['finding_id']}  {f['title']}"
        )
    for c in cases:
        print(f"  case          {c['case_id']}  {c['title']}")
    if not args.apply:
        print("\nDry run: nothing written. Pass --apply to write.")
        return 0

    from core.storage.connection import init_database
    from core.storage.service import DatabaseService

    init_database()
    db = DatabaseService()
    made_findings = made_cases = 0
    for f in findings:
        if db.get_finding(f["finding_id"]) is not None:
            continue
        created = db.create_finding(
            finding_id=f["finding_id"],
            mitre_predictions=f["mitre_predictions"],
            anomaly_score=f["anomaly_score"],
            timestamp=_naive_utc(f["timestamp"]),
            data_source=f["data_source"],
            external_id=f["external_id"],
            title=f["title"],
            description=f["description"],
            entity_context=f["entity_context"],
            cluster_id=f["cluster_id"],
            severity=f["severity"],
        )
        if created is None:
            print(f"  failed to create {f['finding_id']}", file=sys.stderr)
            continue
        updates = {}
        if f.get("noise_marked"):
            updates = {
                "noise_marked_at": _naive_utc(f["noise_marked_at"]),
                "noise_marked_by": f["noise_marked_by"],
                "noise_marked_by_kind": f["noise_marked_by_kind"],
                "noise_reason": f["noise_reason"],
            }
        db.write_ai_enrichment(
            f["finding_id"], f["ai_enrichment"], daemon=True, **updates
        )
        made_findings += 1
    for c in cases:
        if db.get_case(c["case_id"]) is not None:
            continue
        created = db.create_case(
            case_id=c["case_id"],
            title=c["title"],
            finding_ids=c["finding_ids"],
            description=c["description"],
            status=c["status"],
            priority=c["priority"],
            tags=c["tags"],
        )
        if created is None:
            print(f"  failed to create {c['case_id']}", file=sys.stderr)
            continue
        made_cases += 1
    print(f"\nWrote {made_findings} findings and {made_cases} cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
