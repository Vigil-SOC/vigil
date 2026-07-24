"""Which services ``./start.sh`` brings up without being asked.

Stored as a plain text file at the repo root — one service name per line, ``#``
comments ignored. Resolution order mirrors ``services/runtime_config.py``'s
DB -> env -> default layering: **file -> AUTOSTART_SERVICES -> DEFAULT**, with
the file as the UI-writable live source of truth and the env var as the
operator pin for CI/hardened deploys.

The storage choice is forced by bootstrapping, not taste:

- **Not the database.** ``postgres`` is itself on the list, so a list living in
  postgres cannot be read before postgres is up. Circular.
- **Not the secrets store.** It is Fernet over ``~/.vigil/secrets.enc`` —
  unreadable from bash, and an autostart list is not a secret.
- **Not ``.env``.** Bash gets it free, but the backend would have to rewrite the
  file that holds every credential in the system from a request handler, with
  no locking and no atomic replace.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import List

from services.service_manager import REQUIRED_SERVICES, SERVICES

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOSTART_FILE = REPO_ROOT / ".vigil-autostart"

DEFAULT: tuple[str, ...] = ("postgres", "redis", "bifrost", "ollama")

_HEADER = """\
# Services Vigil starts automatically (./start.sh). One name per line.
# Managed by Settings -> Services; hand-edits are preserved.
# Valid names: {valid}
"""


def _known(names: List[str], source: str) -> List[str]:
    out = []
    for n in names:
        if n in SERVICES:
            out.append(n)
        else:
            logger.warning("Ignoring unknown service %r in %s", n, source)
    return out


def _read_file() -> List[str] | None:
    try:
        raw = AUTOSTART_FILE.read_text()
    except OSError:
        return None
    names = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return _known(names, str(AUTOSTART_FILE))


def _with_required(names: List[str]) -> List[str]:
    """Required services first, then the rest, deduped. The app can't boot
    without postgres/redis/bifrost, so they always autostart even if a
    hand-edited file, env var, or API caller leaves them out."""
    return list(dict.fromkeys([*REQUIRED_SERVICES, *names]))


def get_autostart_services() -> List[str]:
    from_file = _read_file()
    if from_file is not None:
        base = from_file
    else:
        env = os.getenv("AUTOSTART_SERVICES")
        if env:
            base = _known(
                [n.strip() for n in env.replace(",", " ").split()],
                "AUTOSTART_SERVICES",
            )
        else:
            base = list(DEFAULT)
    return _with_required(base)


def set_autostart_services(names: List[str]) -> List[str]:
    """Persist the list. Raises ValueError on an unknown service name.

    Required services are folded in unconditionally, so a UI/API caller can't
    persist a list that omits them and bricks the next launch."""
    unknown = [n for n in names if n not in SERVICES]
    if unknown:
        raise ValueError(f"Unknown service(s): {', '.join(unknown)}")
    deduped = _with_required(names)
    body = _HEADER.format(valid=", ".join(SERVICES)) + "\n".join(deduped) + "\n"
    # Atomic replace: a torn write here would silently change what boots.
    fd, tmp = tempfile.mkstemp(dir=str(REPO_ROOT), prefix=".vigil-autostart.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.replace(tmp, AUTOSTART_FILE)
    except OSError:
        os.path.exists(tmp) and os.unlink(tmp)
        raise
    return deduped
