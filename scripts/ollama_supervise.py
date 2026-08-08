#!/usr/bin/env python3
"""CLI: spawn the host-native Ollama, then refresh the Bifrost model catalog.

scripts/lib.sh shells into this rather than reimplementing the spawn: macOS
ships no ``setsid``, and a bare ``nohup ... &`` would leave Ollama in
start.sh's process group where Ctrl+C kills it. Keeping one implementation of
the probe/pidfile/session semantics means one place to get them right.

This is a composition root: it is allowed to depend on both the platform
supervisor and the LLM catalog, which must not depend on each other.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.llm.bifrost.admin import sync_after_ollama_start  # noqa: E402
from core.platform import ollama_supervisor  # noqa: E402
from core.platform.service_manager import SERVICES  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = ollama_supervisor.start(
        SERVICES["ollama"], post_start_sync=sync_after_ollama_start
    )
    print(result.message)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
