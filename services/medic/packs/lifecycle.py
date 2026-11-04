"""The pack lifecycle (F6): every way the active pack changes besides a plain load.

Built on loader.py (check, persist) and, like it, a set of functions with
everything as a parameter: no env, no clock, no config, no decision log, no engine.
Each operation returns an Outcome: refusal codes, or the `pack_event` dicts
(decision-record.schema.json) for the caller to record.

    start()               bundled seed/upgrade
    import_pack()         admin import; a withdrawn version needs `override`
    revert()              last-known-good becomes active
    switch_to_bundled()   the release's bundled pack becomes active
    pack_state()          the API's PackState (minus suppression_7d), the one place it is computed

Every pack is held to pack_check, on start, revert and switch as on import; a stored
pack is not re-hashed against anything. On disk, next to loader.py's active.json /
last-known-good.json, under <data_dir>/packs/ (0600):
    state.json            withdrawn, and per-pack provenance (source, imported_by,
                          activated_at) keyed by the sha256 of the pack file
A pre-2.0 install may leave a trust-root.dsse.json there, a revoked_notice in
state.json and a medic.signed-pack/v1 active file: the first two are ignored, and the
last reads as no active pack, so the bundled pack seeds.

Provenance is keyed by file hash, so after a crash between a swap and its state write an
entry that no longer matches is simply not found: source is then derived by comparing the
file with the bundled bytes, imported_by is null, and activated_at is the file's mtime.
Every write is temp -> fsync -> replace -> fsync-dir. State is written before the swap it
describes, so every crash point leaves a loadable active pack and loses no withdrawn entry.
A crash in switch_to_bundled between the swap and the last-known-good unlink leaves that
(unwithdrawn, still checked) file as a revert target. A crash between the state write and the swap of a revert/switch leaves the still-active
pack listed as withdrawn; _load_state() ignores withdrawn entries matching the active pack
and every operation re-reads state before it swaps, so the entry never comes back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from services.medic.contracts.pack_check import Lineage, check_pack
from services.medic.packs import loader
from services.medic.packs.loader import (
    ACTIVE,
    LAST_KNOWN_GOOD,
    _fsync_dir,
    _write_temp,
    load_pack,
    packs_dir,
    read_active,
    read_last_known_good,
    vet_pack,
)

log = logging.getLogger("services.medic")

STATE_FILE = "state.json"
TOLERATED = frozenset({"P-EXPIRED", "P-VIGIL"})  # all the bundled pack may be let off
WITHDRAWN_CAP = 50
# Lifecycle refusal codes (#2026 adds them to the OpenAPI refusal codes).
WITHDRAWN = "P-WITHDRAWN"
NO_TARGET = "P-NO-TARGET"
NO_ACTIVE = "P-NO-ACTIVE"
BUNDLED_ACTIVE = "P-BUNDLED-ACTIVE"
SIGNED_PACK = "medic.signed-pack/v1"  # pre-2.0 active file: read as no active pack


@dataclass
class Outcome:
    errors: list[tuple[str, str]] = field(default_factory=list)  # (code, message)
    events: list[dict] = field(default_factory=list)  # pack_event dicts, in order

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> list[str]:
        return [c for c, _ in self.errors]


# ---- small helpers -------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sv(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def _ref(lin: Lineage | dict) -> dict:
    g = (lambda k: lin[k]) if isinstance(lin, dict) else (lambda k: getattr(lin, k))
    return {"id": g("id"), "version": g("version")}


def _refuse_all(errors: list[tuple[str, str]]) -> Outcome:
    for code, msg in errors:
        log.warning("pack lifecycle refused %s: %s", code, msg)
    return Outcome(errors=errors)


def _refuse(code: str, msg: str) -> Outcome:
    return _refuse_all([(code, msg)])


def _dir(data_dir: Path) -> Path:
    d = packs_dir(data_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _replace(d: Path, name: str, data: bytes) -> None:
    """Durably put `data` at d/name: the old file is whole until the rename."""
    tmp = _write_temp(d, data)
    try:
        os.replace(tmp, d / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(d)


def _skipped(pack: dict, rules: list[str]) -> list[dict]:
    return [
        {
            "event": "rule_skipped",
            "pack": pack,
            "rule": r,
            "reason": "needs_engine_minor",
        }
        for r in rules
    ]


# ---- state file ----------------------------------------------------------------


def _load_state(data_dir: Path) -> dict:
    try:
        raw = json.loads((packs_dir(data_dir) / STATE_FILE).read_bytes())
        raw = raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError):
        log.error("pack state file unreadable; starting from empty state")
        raw = {}
    st = {
        **raw,  # keys this version doesn't read (a pre-2.0 revoked_notice) are kept
        "withdrawn": list(raw.get("withdrawn") or []),
        "packs": dict(raw.get("packs") or {}),
    }
    active = read_active(data_dir)
    if active:  # see the module docstring: the active pack is never withdrawn
        st["withdrawn"] = [w for w in st["withdrawn"] if w != _ref(active.lineage)]
    return st


def _save_state(data_dir: Path, st: dict, *also_keep: bytes) -> None:
    """Persist; provenance is kept only for the files on disk now and `also_keep`."""
    keep = {
        _sha(s.data)
        for s in (read_active(data_dir), read_last_known_good(data_dir))
        if s
    }
    keep |= {_sha(b) for b in also_keep}
    out = {**st, "packs": {k: v for k, v in st["packs"].items() if k in keep}}
    out["withdrawn"] = out["withdrawn"][-WITHDRAWN_CAP:]
    _replace(_dir(data_dir), STATE_FILE, json.dumps(out, indent=2).encode())


def _withdraw(st: dict, lin: Lineage) -> None:
    ref = _ref(lin)
    st["withdrawn"] = [w for w in st["withdrawn"] if w != ref] + [ref]


def _provenance(data: bytes, st: dict, bundled: bytes | None, path: Path) -> dict:
    e = st["packs"].get(_sha(data))
    if e:
        return e
    try:  # no matching entry (crash, or never recorded): derive it
        at = _iso(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    except OSError:
        at = "1970-01-01T00:00:00Z"
    src = "bundled" if bundled is not None and data == bundled else "admin_import"
    return {"source": src, "imported_by": None, "activated_at": at}


# ---- startup: seed, upgrade ------------------------------------------------------


def start(
    *, data_dir: Path, bundled: bytes | None, vigil_version: str, now: datetime
) -> Outcome:
    """Run once at startup. Errors are why the bundled pack was not seeded or
    upgraded; the active pack, or only the watcher.* rules, keep running."""
    d = _dir(data_dir)
    for tmp in d.glob(".pack.*"):  # temp files a crash left behind
        tmp.unlink(missing_ok=True)
    if _is_signed_pack(d / ACTIVE):
        log.warning("active pack is a pre-2.0 signed pack; read as no active pack")
    out = Outcome()
    _seed_or_upgrade(data_dir, bundled, out, vigil_version=vigil_version, now=now)
    for code in dict.fromkeys(out.codes):  # once per code
        msg = next(m for c, m in out.errors if c == code)
        log.error("bundled pack not seeded: %s %s", code, msg)
    return out


def _is_signed_pack(path: Path) -> bool:
    try:
        doc = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return False
    return isinstance(doc, dict) and doc.get("format") == SIGNED_PACK


def _seed_or_upgrade(data_dir, bundled, out, *, vigil_version, now) -> None:
    if bundled is None:
        return
    active = read_active(data_dir)
    st = _load_state(data_dir)
    if active:
        b = loader._lineage(bundled)
        if (
            b is None
            or (b.id, b.channel) != (active.lineage.id, active.lineage.channel)
            or _sv(b.version) <= _sv(active.lineage.version)
        ):
            return  # lower, equal, other pack: the active pack stays
        if _ref(b) in st["withdrawn"]:
            log.info("bundled %s is withdrawn on this site; not upgrading", _ref(b))
            return
    res = load_pack(
        bundled, data_dir=data_dir, vigil_version=vigil_version, now=now,
        tolerate=TOLERATED,
    )  # fmt: skip
    if not res.ok:
        out.errors += res.errors
        return
    for code, msg in res.tolerated:
        log.warning("bundled pack installed despite %s: %s", code, msg)
    st["packs"][_sha(bundled)] = {
        "source": "bundled",
        "imported_by": None,
        "activated_at": _iso(now),
    }
    _save_state(data_dir, st)
    out.events += _activated(res, "bundled")


def _activated(
    res: loader.LoadResult, source: str, by: str | None = None
) -> list[dict]:
    i = res.imported
    pack = {"id": i["id"], "version": i["version"]}
    ev = {"event": "imported", "pack": pack, "previous": i["previous"] and _ref(i["previous"]),
          "source": source}  # fmt: skip
    if by is not None:
        ev["by"] = by
    return [ev, *_skipped(pack, res.rule_skipped)]


# ---- admin operations ------------------------------------------------------------


def import_pack(
    data: bytes,
    *,
    data_dir: Path,
    vigil_version: str,
    now: datetime,
    by: str,
    override: bool = False,
) -> Outcome:
    """Admin import. A version on `withdrawn` is refused unless `override`, which does
    nothing for a version that isn't withdrawn."""
    st = _load_state(data_dir)
    lin = loader._lineage(data)
    withdrawn = lin is not None and _ref(lin) in st["withdrawn"]
    if withdrawn and not override:
        return _refuse(
            WITHDRAWN,
            f"{lin.id} {lin.version} was withdrawn here; override to re-import",
        )
    previous = read_active(data_dir)
    res = load_pack(
        data, data_dir=data_dir, vigil_version=vigil_version, now=now,
        override_lineage=withdrawn,
    )  # fmt: skip
    if not res.ok:
        return Outcome(errors=res.errors)
    st["packs"][_sha(data)] = {
        "source": "admin_import",
        "imported_by": by,
        "activated_at": _iso(now),
    }
    if withdrawn:
        st["withdrawn"].remove(_ref(lin))
    _save_state(data_dir, st)
    events = _activated(res, "admin_import", by)
    if withdrawn and previous:
        # `override` has no `source`, and `previous` is required (never null) there.
        ev = events[0]
        events[0] = {
            "event": "override",
            "pack": ev["pack"],
            "previous": ev["previous"],
            "by": by,
        }
    return Outcome(events=events)


def revert(
    *,
    data_dir: Path,
    bundled: bytes | None,
    vigil_version: str,
    now: datetime,
    by: str,
) -> Outcome:
    """Last-known-good becomes active (re-checked with pack_check, strictly);
    the version reverted away from is withdrawn; last-known-good is left empty."""
    active, target = read_active(data_dir), read_last_known_good(data_dir)
    if active is None:
        return _refuse(NO_ACTIVE, "no active pack to revert from")
    if (
        target is None or target.data == active.data
    ):  # (a crashed import can leave a copy)
        return _refuse(NO_TARGET, "no last-known-good pack to revert to")
    errors, res = vet_pack(
        target.data, vigil_version=vigil_version, now=now, lineage=None,
        override_lineage=True,
    )  # fmt: skip
    if errors:
        return _refuse_all(errors)
    st = _load_state(data_dir)
    _withdraw(st, active.lineage)
    prov = _provenance(target.data, st, bundled, packs_dir(data_dir) / LAST_KNOWN_GOOD)
    st["packs"][_sha(target.data)] = {**prov, "activated_at": _iso(now)}
    _save_state(data_dir, st)  # state first
    d = packs_dir(data_dir)
    os.replace(
        d / LAST_KNOWN_GOOD, d / ACTIVE
    )  # one rename: always a loadable active pack
    _fsync_dir(d)
    return _switched(res, active, by)


def switch_to_bundled(
    *,
    data_dir: Path,
    bundled: bytes | None,
    vigil_version: str,
    now: datetime,
    by: str,
) -> Outcome:
    """The bundled pack becomes active (re-checked, with its exception); the version
    switched away from is withdrawn; last-known-good is left empty."""
    active = read_active(data_dir)
    if bundled is None:
        return _refuse(NO_TARGET, "this release has no bundled pack")
    if active is None:
        return _refuse(NO_ACTIVE, "no active pack to switch from")
    if active.data == bundled:
        return _refuse(BUNDLED_ACTIVE, "the bundled pack is already active")
    errors, res = vet_pack(
        bundled, vigil_version=vigil_version, now=now, lineage=None,
        override_lineage=True, tolerate=TOLERATED,
    )  # fmt: skip
    if errors:
        return _refuse_all(errors)
    st = _load_state(data_dir)
    _withdraw(st, active.lineage)
    st["packs"][_sha(bundled)] = {
        "source": "bundled",
        "imported_by": None,
        "activated_at": _iso(now),
    }
    _save_state(data_dir, st, bundled)  # state first
    d = packs_dir(data_dir)
    _replace(d, ACTIVE, bundled)
    (d / LAST_KNOWN_GOOD).unlink(missing_ok=True)
    _fsync_dir(d)
    return _switched(res, active, by)


def _switched(res, outgoing: loader.StoredPack, by: str) -> Outcome:
    pack = _ref(res.manifest["pack"])
    ev = {
        "event": "reverted",
        "pack": pack,
        "previous": _ref(outgoing.lineage),
        "by": by,
    }
    log.info("pack reverted: %s -> %s", ev["previous"], pack)
    return Outcome(events=[ev, *_skipped(pack, res.skipped_rules)])


# ---- PackState -------------------------------------------------------------------


def _facts(data: bytes, prov: dict, vigil_version: str, now: datetime) -> dict | None:
    """PackFacts from a stored file; stale / in_vigil_range / rules_skipped are derived now."""
    try:
        m = json.loads(data)["manifest"]
        res = check_pack(data, vigil_version=vigil_version, now=now)
        return {
            "pack": m["pack"],
            "sha256": _sha(data),
            "vigil": m["vigil"],
            "source": prov["source"],
            "imported_by": prov["imported_by"],
            "activated_at": prov["activated_at"],
            "stale": "P-EXPIRED" in res.codes,
            "in_vigil_range": "P-VIGIL" not in res.codes,
            "rules_skipped": len(res.skipped_rules),
        }
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        log.error("stored pack file has no readable manifest")
        return None


def pack_state(
    *,
    data_dir: Path,
    bundled: bytes | None,
    vigil_version: str,
    now: datetime,
) -> dict:
    """PackState from medic-api.openapi.yaml, without `suppression_7d` (a decision-store
    read, the API's). `bundled` is null while it is the active pack; its activated_at is
    the pack's created_at, as it has not been activated."""
    st = _load_state(data_dir)
    d = packs_dir(data_dir)

    def stored(sp: loader.StoredPack | None, name: str) -> dict | None:
        if sp is None:
            return None
        return _facts(
            sp.data, _provenance(sp.data, st, bundled, d / name), vigil_version, now
        )

    active = read_active(data_dir)
    bundled_facts = None
    if bundled is not None and not (active and active.data == bundled):
        lin = loader._lineage(bundled)
        if lin:
            prov = {"source": "bundled", "imported_by": None, "activated_at": ""}
            bundled_facts = _facts(bundled, prov, vigil_version, now)
            if bundled_facts:
                bundled_facts["activated_at"] = bundled_facts["pack"]["created_at"]
    return {
        "active": stored(active, ACTIVE),
        "previous": stored(read_last_known_good(data_dir), LAST_KNOWN_GOOD),
        "bundled": bundled_facts,
        "withdrawn": st["withdrawn"][-WITHDRAWN_CAP:],
    }
