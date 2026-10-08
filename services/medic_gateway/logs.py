"""JSON log lines through a redacting filter (C3 §4.6: Medic reads these logs).

Call sites log fixed event names and short codes, never a target, header or body.
The filter is the second layer: it replaces every secret the gateway has seen
(password, tokens), anything JWT- or bearer-shaped, and drops tracebacks.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

LOGGER = "services.medic_gateway"
_SECRETS: set[str] = set()
_LOCK = threading.Lock()
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*(\.[A-Za-z0-9_-]*)?")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message"}

log = logging.getLogger(LOGGER)


def remember(*values: str | None) -> None:
    with _LOCK:
        _SECRETS.update(v for v in values if v)


def forget(value: str | None) -> None:
    with _LOCK:
        _SECRETS.discard(value)


def redact(text: str) -> str:
    text = _BEARER.sub("Bearer [redacted]", _JWT.sub("[jwt]", text))
    with _LOCK:
        known = sorted(_SECRETS, key=len, reverse=True)
    for s in known:
        text = text.replace(s, "[secret]")
    return text


def _clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    return redact(json.dumps(value, default=str))


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), ()
        record.exc_info = record.exc_text = record.stack_info = None
        for k in set(vars(record)) - _RESERVED:
            setattr(record, k, _clean(getattr(record, k)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        line.update({k: v for k, v in vars(record).items() if k not in _RESERVED})
        return json.dumps(line, sort_keys=True)


def setup(stream) -> logging.Handler:
    """One handler on the gateway's logger; replaces any earlier one."""
    for h in [h for h in log.handlers if getattr(h, "medic_gateway", False)]:
        log.removeHandler(h)
    handler = logging.StreamHandler(stream)
    handler.medic_gateway = True
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return handler


def event(name: str, level: int = logging.INFO, **fields) -> None:
    log.log(level, name, extra=fields)
