"""The pack admin operations, run by the main loop (#2026): the one writer.

The API thread vets and queues (api/pack.py). At each cycle boundary the loop calls
`apply_pending`: each queued request runs through the lifecycle, its events go to
the decision log as `pack_event`s, and the engine, router, suppression list and log
catalogue are rebuilt from the new active pack. `state` is the `PackState` the loop
publishes with each snapshot.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.medic.api.decisions import Problem
from services.medic.api.pack import PackBridge, refusal, suppression_7d
from services.medic.app.wiring import Loaded, PackContext, reload_active
from services.medic.engine.core import iso
from services.medic.packs import lifecycle

log = logging.getLogger("services.medic")

SUPPRESSION_REFRESH_S = 60  # a scan of the decision store: not every 15 s tick


class PackAdmin:
    def __init__(
        self, loaded: Loaded, *, shape: str, data_dir: Path, clock, monotonic=None
    ) -> None:
        assert loaded.context is not None
        self.context: PackContext = loaded.context
        self.shape, self.data_dir, self.clock = shape, data_dir, clock
        self.facts = loaded.facts  # the active pack's PackFacts (`/v1/status.pack`)
        self.bridge = PackBridge(self.context.root, self.context.vigil)
        self._monotonic = monotonic or time.monotonic
        self._suppression: tuple[float, list[dict[str, Any]]] | None = None
        self._answered: list = []

    def state(self, now: datetime, *, fresh: bool = False) -> dict[str, Any]:
        """`PackState`. `fresh` re-reads the suppression counts (after a swap)."""
        at = self._monotonic()
        if (
            fresh
            or self._suppression is None
            or at - self._suppression[0] >= (SUPPRESSION_REFRESH_S)
        ):
            self._suppression = (at, suppression_7d(self.data_dir, now))
        ctx = self.context
        return {
            **lifecycle.pack_state(
                data_dir=self.data_dir,
                trust_root=ctx.root,
                bundled=ctx.bundled,
                vigil_version=ctx.vigil,
                now=now,
            ),
            "suppression_7d": self._suppression[1],
        }

    def apply_pending(self, medic) -> None:
        """Run every request still wanted. Never raises: a failure is that
        request's answer. The callers wait until `release`, so what they are told
        is already what `GET /v1/pack` and `/v1/status` serve."""
        for req in self.bridge.claim():
            try:
                req.result = self._apply(req, medic)
            except Exception as exc:  # noqa: BLE001 (the loop keeps beating)
                log.error("pack %s failed: %s", req.op, type(exc).__name__)
                req.result = Problem(500, "internal")
            self._answered.append(req)

    def release(self) -> None:
        """Wake the callers, once the snapshot carrying the new state is published."""
        answered, self._answered = self._answered, []
        for req in answered:
            req.done.set()

    def _apply(self, req, medic) -> dict[str, Any] | Problem:
        ctx = self.context
        now = datetime.fromtimestamp(self.clock.wall(), UTC)
        if req.op == "acknowledge":  # no chain event, only a log line
            notice = lifecycle.acknowledge_revoked_notice(self.data_dir)
            log.info(
                "pack revoked-key notice %s by %s",
                "acknowledged" if notice else "was not set",
                req.by,
            )
            return self.state(now, fresh=True)
        # Checked before the swap, so a pack is never swapped and left unrecorded.
        if medic.writer.stopped:
            return Problem(503, "store_not_recording")
        kw: dict[str, Any] = {
            "data_dir": self.data_dir,
            "trust_root": ctx.root,
            "vigil_version": ctx.vigil,
            "dev_mode": False,
            "now": now,
            "by": req.by,
        }
        if req.op == "import":
            out = lifecycle.import_pack(req.data, override=req.override, **kw)
        elif req.op == "revert":
            out = lifecycle.revert(bundled=ctx.bundled, **kw)
        else:
            out = lifecycle.switch_to_bundled(bundled=ctx.bundled, **kw)
        if not out.ok:  # logged by the lifecycle; never a pack_event
            return refusal(out.errors)
        for event in out.events:
            medic._store(
                {
                    "v": 1,
                    "at": iso(now.timestamp()),
                    "type": "pack_event",
                    "body": event,
                }
            )
        loaded = reload_active(self.shape, self.data_dir, now, ctx)
        medic.swap(loaded)
        self.facts = loaded.facts
        return self.state(now, fresh=True)
