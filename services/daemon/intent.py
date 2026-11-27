"""Intent CLI: INTENT.md declared values beside the effective ones.

Run from a shell without a daemon:  python -m services.daemon.intent

Read-only. The report itself is ``core.intent_report``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence

from core.intent import format_rows
from core.intent_report import intent_report, report_path

logger = logging.getLogger(__name__)


def _report_main() -> int:
    rows = intent_report()
    path = report_path()
    if rows is None:
        print(f"no intent report: {path} could not be read")
        return 0
    if not rows:
        print(f"{path}: no declared key differs from effective daemon config")
        return 0
    print(f"{path}: {len(rows)} key(s) differ from effective daemon config")
    for line in format_rows(rows):
        print(line)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    argparse.ArgumentParser(prog="python -m services.daemon.intent").parse_args(argv)
    return _report_main()


if __name__ == "__main__":
    sys.exit(main())
