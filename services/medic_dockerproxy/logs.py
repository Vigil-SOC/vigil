"""JSON log lines (C3 §4.6 style: Medic reads these logs).

Call sites log fixed event names and short codes only: never a target, a header
or a Docker body, so there is nothing here for a redacting filter to catch.
"""

from __future__ import annotations

import json
import logging
import time

LOGGER = "services.medic_dockerproxy"
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message"}

log = logging.getLogger(LOGGER)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        line.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        return json.dumps(line, sort_keys=True, default=str)


def setup(stream) -> logging.Handler:
    """One handler on the proxy's logger; replaces any earlier one."""
    for h in [h for h in log.handlers if getattr(h, "medic_dockerproxy", False)]:
        log.removeHandler(h)
    handler = logging.StreamHandler(stream)
    handler.medic_dockerproxy = True
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return handler


def event(name: str, level: int = logging.INFO, **fields) -> None:
    log.log(level, name, extra=fields)
