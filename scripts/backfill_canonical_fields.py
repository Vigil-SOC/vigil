"""Backfill canonical entity fields for existing findings.

Reads every finding that has a non-null entity_context but null canonical
fields, extracts the canonical values, and writes them back.

Safe to run multiple times — only processes findings where the core
identifying canonical fields (src_ip, dst_ip, hostname, username) are all
null, indicating the row has not yet been backfilled.

Usage:
    source venv/bin/activate
    python scripts/backfill_canonical_fields.py [--dry-run] [--batch-size 500]
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_CANONICAL = ('src_ip', 'dst_ip', 'hostname', 'username', 'process_name', 'file_hash', 'alert_category')


def run(dry_run: bool = False, batch_size: int = 500):
    from sqlalchemy.orm import Session
    from database.connection import get_db_manager
    from database.models import Finding
    from services.ingestion_service import extract_canonical_fields

    db = get_db_manager()
    db.initialize()
    if not db.health_check():
        logger.error("Database not reachable.")
        sys.exit(1)

    engine = db._engine
    updated = skipped = errors = 0

    with Session(engine) as session:
        # Only backfill rows that have entity_context but no canonical fields yet
        rows = (
            session.query(Finding)
            .filter(
                Finding.entity_context.isnot(None),
                Finding.src_ip.is_(None),
                Finding.dst_ip.is_(None),
                Finding.hostname.is_(None),
                Finding.username.is_(None),
            )
            .all()
        )
        logger.info("Found %d findings to backfill", len(rows))

        for finding in rows:
            try:
                entity_ctx = finding.entity_context or {}
                mitre_preds = finding.mitre_predictions or {}
                canon = extract_canonical_fields(entity_ctx, mitre_preds)

                has_value = any(canon.get(f) for f in _CANONICAL)
                if not has_value:
                    skipped += 1
                    continue

                if not dry_run:
                    for field in _CANONICAL:
                        val = canon.get(field)
                        if val is not None:
                            setattr(finding, field, val)
                    if canon.get('raw_fields') is not None:
                        finding.raw_fields = canon['raw_fields']

                updated += 1

                if updated % batch_size == 0:
                    if not dry_run:
                        session.commit()
                    logger.info("  %d updated, %d skipped, %d errors so far", updated, skipped, errors)

            except Exception as exc:
                errors += 1
                logger.warning("Error on %s: %s", finding.finding_id, exc)

        if not dry_run:
            session.commit()

    logger.info(
        "Backfill complete — updated: %d, skipped: %d, errors: %d%s",
        updated, skipped, errors, " (DRY RUN)" if dry_run else "",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    parser.add_argument("--batch-size", type=int, default=500, help="Commit every N rows")
    args = parser.parse_args()
    run(dry_run=args.dry_run, batch_size=args.batch_size)
