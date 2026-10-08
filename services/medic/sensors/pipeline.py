"""Bus → redaction choke point → id → schema check → sink. The only way out.

Fail closed: an observation the choke point can't process is dropped and counted
(`redactor_failures`), never written raw; one that fails the schema is dropped and
counted (`dropped`). `id`/`seq` are assigned only to observations that reach the
sink, so a run's seq has no gaps (observation.schema.json `sensor.seq`).
"""

from __future__ import annotations

import json
import logging
import secrets
import string
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

from services.medic.sensors.base import CONTRACTS
from services.medic.sensors.bus import Bus
from services.medic.sensors.sink import Sink

log = logging.getLogger("services.medic.sensors")

_ALNUM = string.ascii_letters + string.digits
VALIDATOR = Draft202012Validator(
    json.loads((CONTRACTS / "observation.schema.json").read_text())
)


# PROVISIONAL (S3-1): samples and sensor_health are validated at runtime (≈ 0.46 ms
# each; under 0.5 % of a core at C7's intervals). Log observations are validated in
# tests only: at C7's 1,000 lines/s flood the same check would cost ≈ 46 % of a core.
RUNTIME_VALIDATED = frozenset({"sample", "sensor_health"})


# Takes a draft (no id yet), returns it redacted with `redaction` stamped.
Choke = Callable[[dict[str, Any]], dict[str, Any]]


def new_run_id() -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(16))


class Pipeline:
    def __init__(
        self,
        bus: Bus,
        sink: Sink,
        choke: Choke,
        *,
        validate_kinds: frozenset[str] = RUNTIME_VALIDATED,
        run_id: Callable[[], str] = new_run_id,
    ) -> None:
        self.bus = bus
        self.sink = sink
        self.choke = choke
        self.validate_kinds = validate_kinds
        self._run_id = run_id
        self._runs: dict[str, str] = {}
        self._seq: dict[str, int] = {}

    def run_of(self, emitter: str) -> str:
        """`sensor.run`: new for every emitter each time Medic starts."""
        if emitter not in self._runs:
            self._runs[emitter] = self._run_id()
        return self._runs[emitter]

    def drain(self) -> int:
        """Write everything queued; return how many observations reached the sink."""
        written = 0
        for draft in self.bus.take():
            written += self._emit(draft)
        return written

    def _emit(self, draft: dict[str, Any]) -> int:
        emitter, subject = draft.pop("_emitter"), draft.pop("_subject")
        stats = self.bus.stats[subject]
        try:
            body = self.choke(draft)
        # Fail closed: whatever goes wrong, drop and count, never write raw.
        except Exception as exc:  # noqa: BLE001
            stats.redactor_failures += 1
            log.warning(
                "Dropped an observation from %s: redaction failed (%s)",
                subject,
                type(exc).__name__,
            )
            return 0
        run, seq = self.run_of(emitter), self._seq.get(emitter, 0)
        obs = {
            "v": 1,
            "id": f"{emitter}:{run}:{seq}",
            "sensor": {"id": emitter, "run": run, "seq": seq},
            **body,
        }
        if obs["kind"] in self.validate_kinds:
            error = next(VALIDATOR.iter_errors(obs), None)
            if error is not None:
                stats.dropped += 1
                # Path and keyword only: the message can quote the value.
                where = "/".join(str(p) for p in error.absolute_path) or "(root)"
                log.warning(
                    "Dropped an invalid observation from %s: %s at %s",
                    subject,
                    error.validator,
                    where,
                )
                return 0
        self.sink.write(obs)
        self._seq[emitter] = seq + 1
        return 1
