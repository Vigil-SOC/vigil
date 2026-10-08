"""The choke point: one observation, or one of Medic's own log records, in; redacted out.

Fail closed is the caller's half for observations (the pipeline drops the payload
and counts it when `redact_observation` raises). For log records the factory does it
here: a record that can't be redacted is replaced by a fixed line, never written raw.
"""

from __future__ import annotations

import copy
import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import re2

from services.medic.contracts.fingerprint_ref import label_safe
from services.medic.redact.rules import REDACTED, REDACTION_VERSION, Redactor

TEXT_MAX = 500  # observation.schema.json untrusted_text
ARG_MAX = 200  # log.args items
WITHHELD = "log record withheld: redactor failed"
# Redaction time grows with input; anything past this can't survive the 500-char
# cap anyway, so it's cut first (the cut can't leave a fragment in the output).
INPUT_MAX = 65_536
# "user:password@host" with no scheme: the URL rule needs "://", and the label
# pattern allows ":" and "@", so a label would keep it verbatim.
_USERINFO = re2.compile(r"[^@/\s]*:[^@/\s]*@")


def redact_observation(obs: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    """Return a redacted copy with `redaction` stamped. Every free-text field is
    redacted in full and then capped. Labels, enum values and `target.instance`
    are redacted and then made label-safe (sensors pass them raw). A log's
    `exc_type`, `logger` or frame that held a secret is removed."""
    out = copy.deepcopy(obs)
    hits = 0

    def text(value: str | None, cap: int = TEXT_MAX) -> str | None:
        nonlocal hits
        if value is None:
            return None
        red, n = redactor.redact(value[:INPUT_MAX])
        hits += n
        return red[:cap]

    def label(value: str) -> str:
        # Redact first, then D3's label rule, once: a value that doesn't fit the
        # pattern is hashed, and the hash is of the redacted text, never a secret.
        nonlocal hits
        if (m := _USERINFO.match(value)) is not None:
            value = REDACTED + "@" + value[m.end() :]
            hits += 1
        red, n = redactor.redact(value[:INPUT_MAX])
        hits += n
        return label_safe(red)

    target = out.get("target", {})
    if "instance" in target:
        target["instance"] = label(target["instance"])
    for value in out.get("values", []):
        if value["type"] == "text":
            value["value"] = text(value["value"])
        elif value["type"] == "enum" and isinstance(value["value"], str):
            value["value"] = label(value["value"])
        if "labels" in value:
            value["labels"] = {k: label(v) for k, v in value["labels"].items()}
    for err in (out.get("error"), out.get("health", {}).get("last_error")):
        if err and "detail" in err:
            err["detail"] = text(err["detail"])
    log = out.get("log")
    if log:
        for key in ("template", "message"):
            if key in log:
                log[key] = text(log[key])
        if "args" in log:
            log["args"] = [text(a, ARG_MAX) for a in log["args"]]
        for key in ("logger", "exc_type"):
            if log.get(key) and redactor.redact(log[key])[1]:
                hits += 1
                log[key] = None
        if "frames" in log:
            kept = [f for f in log["frames"] if not redactor.redact(f)[1]]
            hits += len(log["frames"]) - len(kept)
            log["frames"] = kept
    out["redaction"] = {"version": REDACTION_VERSION, "hits": hits}
    return out


@dataclass
class LogRedaction:
    """Handle returned by `install_log_redaction`; `failures` counts withheld records."""

    failures: int = 0
    _previous: Callable[..., logging.LogRecord] | None = field(default=None, repr=False)

    def uninstall(self) -> None:
        if self._previous is not None:
            logging.setLogRecordFactory(self._previous)
            self._previous = None


def install_log_redaction(redactor: Redactor | None = None) -> LogRedaction:
    """Redact every log record this process creates, whatever handler writes it.

    A record factory rather than a handler filter, so a handler configured later
    can't bypass it. The message is rendered, redacted and frozen (`args` cleared);
    a traceback or stack is formatted, redacted and kept as text. `extra` fields
    are set after the factory runs, so Medic code doesn't pass data in `extra`
    (tests/redact/test_log_redaction.py enforces it).
    """
    redactor = redactor or Redactor()
    previous = logging.getLogRecordFactory()
    handle = LogRedaction(_previous=previous)

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            _scrub(record, redactor)
        # Fail closed: any redactor bug withholds the record.
        except Exception:  # noqa: BLE001
            handle.failures += 1
            record.msg, record.args = WITHHELD, ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    logging.setLogRecordFactory(factory)
    return handle


def _scrub(record: logging.LogRecord, redactor: Redactor) -> None:
    message = redactor.redact(record.getMessage()[:INPUT_MAX])[0]
    exc_text = None
    if record.exc_info and record.exc_info[0] is not None:
        exc_text = redactor.redact(
            "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        )[0]
    stack = redactor.redact(record.stack_info)[0] if record.stack_info else None
    record.msg, record.args = message, ()
    record.exc_info, record.exc_text, record.stack_info = None, exc_text, stack
