#!/usr/bin/env python3
"""
Backfill MITRE Tactic Names

One-time migration script that converts mitre_predictions keys from
opaque integer labels (e.g. "mitre_class_7") to human-readable MITRE
ATT&CK tactic names (e.g. "Command and Control").

Usage:
    python scripts/backfill_mitre_tactics.py              # dry-run
    python scripts/backfill_mitre_tactics.py --apply       # apply changes
"""

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.storage.database_data_service import DatabaseDataService
from core.ingestion.ingestion_service import MITRE_TACTIC_MAP

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

MITRE_CLASS_RE = re.compile(r'^mitre_class_(\d+)$')


def backfill(apply: bool = False):
    data_service = DatabaseDataService()
    findings = data_service.get_findings()

    if isinstance(findings, dict) and 'error' in findings:
        logger.error(f"Could not load findings: {findings['error']}")
        return

    candidates = []
    for f in findings:
        preds = f.get('mitre_predictions') or {}
        needs_update = any(MITRE_CLASS_RE.match(k) for k in preds)
        if needs_update:
            candidates.append(f)

    logger.info(f"Found {len(candidates)} findings with mitre_class_* keys (out of {len(findings)} total)")

    if not candidates:
        logger.info("Nothing to backfill.")
        return

    updated = 0
    skipped = 0
    for f in candidates:
        finding_id = f['finding_id']
        old_preds = f.get('mitre_predictions', {})
        new_preds = {}

        for key, score in old_preds.items():
            m = MITRE_CLASS_RE.match(key)
            if m:
                class_idx = int(m.group(1))
                tactic = MITRE_TACTIC_MAP.get(class_idx)
                if tactic:
                    new_preds[tactic] = score
                else:
                    new_preds[key] = score
                    logger.warning(f"  {finding_id}: unknown class index {class_idx}, keeping '{key}'")
            else:
                new_preds[key] = score

        if new_preds == old_preds:
            skipped += 1
            continue

        logger.info(f"  {finding_id}: {old_preds} -> {new_preds}")

        if apply:
            ok = data_service.update_finding(finding_id, mitre_predictions=new_preds)
            if ok:
                updated += 1
            else:
                logger.error(f"  Failed to update {finding_id}")
        else:
            updated += 1

    mode = "Updated" if apply else "Would update"
    logger.info(f"{mode} {updated} findings, skipped {skipped}")
    if not apply and updated > 0:
        logger.info("Run with --apply to persist changes.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill MITRE tactic names in findings')
    parser.add_argument('--apply', action='store_true', help='Actually write changes (default is dry-run)')
    args = parser.parse_args()
    backfill(apply=args.apply)
