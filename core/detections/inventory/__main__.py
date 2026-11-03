"""``python -m core.detections.inventory [source ...]``: sync once, print the report."""

import json
import sys
from dataclasses import asdict

from core.detections.inventory.sync import sync_all


def main(argv: list[str]) -> int:
    reports = sync_all(argv or None)
    for report in reports:
        row = asdict(report)
        # Keys are for the reader; ten of each is enough to check by eye.
        row["versioned_keys"] = row["versioned_keys"][:10]
        row["removed_keys"] = row["removed_keys"][:10]
        print(json.dumps(row))
    return 1 if any(r.error for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
