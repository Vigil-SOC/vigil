"""The pack loader (F6): check, persist atomically, keep last-known-good.

load_pack() takes the raw bytes of one medic.pack/v1 file and either makes it the
active pack (the outgoing active pack becomes last-known-good) or refuses it with
pack_check's own codes and changes nothing on disk. An admin import's expected
sha256 is checked before this, on the API thread (api/pack.py).

`tolerate` (check_pack codes to let through) and `override_lineage` exist for the
lifecycle (lifecycle.py); vet_pack() is the same checks without the write.

A refusal is a return value, never an exception. The loader writes no decision log
and touches no engine: the caller records `imported` / `rule_skipped` as pack_events.
Everything it needs (Vigil version, clock, data dir) is a parameter. A failing disk
is not hostile input, so a write error is raised (after its temp files are cleaned
up) with the previous active pack still in place.

On disk, under <data_dir>/packs/ (dir 0700, files 0600), the pack file as received:
    active.json            the pack in force
    last-known-good.json   the pack it replaced
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from services.medic.contracts.pack_check import Lineage, check_pack

log = logging.getLogger("services.medic")

ACTIVE = "active.json"
LAST_KNOWN_GOOD = "last-known-good.json"
_SEMVER = re.compile(r"\d+\.\d+\.\d+")


@dataclass
class LoadResult:
    errors: list[tuple[str, str]] = field(default_factory=list)  # (code, message)
    imported: dict | None = None  # id, channel, version, sha256, publisher, previous
    rule_skipped: list[str] = field(default_factory=list)
    tolerated: list[tuple[str, str]] = field(default_factory=list)  # let through

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> list[str]:
        return [c for c, _ in self.errors]


@dataclass(frozen=True)
class StoredPack:
    data: bytes  # the pack file exactly as it was imported
    lineage: Lineage


def packs_dir(data_dir: Path) -> Path:
    return data_dir / "packs"


def _lineage(data: bytes) -> Lineage | None:
    """The id/channel/version a stored file claims, or None if it isn't a pack file
    (a pre-2.0 medic.signed-pack/v1 file has no top-level manifest: None)."""
    try:
        manifest = json.loads(data)["manifest"]["pack"]
        lin = Lineage(manifest["id"], manifest["channel"], manifest["version"])
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return None
    ok = all(isinstance(v, str) for v in (lin.id, lin.channel, lin.version))
    return lin if ok and _SEMVER.fullmatch(lin.version) else None


def _read(data_dir: Path, name: str) -> StoredPack | None:
    try:
        data = (packs_dir(data_dir) / name).read_bytes()
    except OSError:
        return None
    lin = _lineage(data)
    return StoredPack(data, lin) if lin else None


def read_active(data_dir: Path) -> StoredPack | None:
    """The active pack, or None if missing or unreadable (never a crash)."""
    return _read(data_dir, ACTIVE)


def read_last_known_good(data_dir: Path) -> StoredPack | None:
    return _read(data_dir, LAST_KNOWN_GOOD)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory: Path, data: bytes) -> str:
    """Durable temp file (mode 0600) in `directory`; removed again if anything fails."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pack.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return tmp


def _persist(data_dir: Path, data: bytes, outgoing: StoredPack | None) -> None:
    """New file to temp; outgoing active to last-known-good; then the new file to active.

    Between the last two renames the old active pack is still in place, so there is
    no instant at which neither an active nor a previous pack can be loaded.
    """
    d = packs_dir(data_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    temps: list[str] = []
    try:
        new_tmp = _write_temp(d, data)
        temps.append(new_tmp)
        if outgoing is not None:
            lkg_tmp = _write_temp(d, outgoing.data)
            temps.append(lkg_tmp)
            os.replace(lkg_tmp, d / LAST_KNOWN_GOOD)
            temps.remove(lkg_tmp)
            _fsync_dir(d)
        os.replace(new_tmp, d / ACTIVE)
        temps.remove(new_tmp)
        _fsync_dir(d)
    except BaseException:
        for t in temps:
            Path(t).unlink(missing_ok=True)
        raise


def _refuse(errors: list[tuple[str, str]]) -> LoadResult:
    for code, msg in errors:
        log.warning("pack refused %s: %s", code, msg)
    return LoadResult(errors=errors)


def vet_pack(
    data: bytes,
    *,
    vigil_version: str,
    now: datetime,
    lineage: Lineage | None,
    override_lineage: bool = False,
    tolerate: frozenset[str] = frozenset(),
):
    """Every check short of writing: (errors, check_pack result).
    `tolerate` names check_pack codes to let through (the bundled pack's exception)."""
    res = check_pack(
        data,
        vigil_version=vigil_version,
        now=now,
        last_known_good=lineage,
        override_lineage=override_lineage,
    )
    return [e for e in res.errors if e[0] not in tolerate], res


def load_pack(
    data: bytes,
    *,
    data_dir: Path,
    vigil_version: str,
    now: datetime,
    override_lineage: bool = False,
    tolerate: frozenset[str] = frozenset(),
) -> LoadResult:
    active = read_active(data_dir)
    errors, res = vet_pack(
        data,
        vigil_version=vigil_version,
        now=now,
        lineage=active.lineage if active else None,
        override_lineage=override_lineage,
        tolerate=tolerate,
    )
    if errors:
        return _refuse(errors)
    pack = res.manifest["pack"]

    _persist(data_dir, data, active)
    imported = {
        "id": pack["id"],
        "channel": pack["channel"],
        "version": pack["version"],
        "sha256": hashlib.sha256(data).hexdigest(),
        "publisher": pack["publisher"],
        "previous": (
            {
                "id": active.lineage.id,
                "channel": active.lineage.channel,
                "version": active.lineage.version,
            }
            if active
            else None
        ),
    }
    log.info("pack imported %s %s/%s", pack["version"], pack["id"], pack["channel"])
    return LoadResult(
        imported=imported,
        rule_skipped=list(res.skipped_rules),
        tolerated=[e for e in res.errors if e[0] in tolerate],
    )
