"""S2: the data root, file modes, the instance id and the cap/reserve stub.

K1 §6 C4: "file mode 0600/0700" and "never the State Directory". C4 §6.1: refuse to
start on a root owned by another uid, readable by another group or by everyone, or
with a symlink under it. X1 ⚑5a: instance_id is random, persisted beside the store.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
from pathlib import Path

import pytest

from services.medic.store import (
    GIB,
    RESERVE_BYTES,
    StoreRefused,
    open_writer,
    verify_store,
)
from services.medic.store.files import mode_problem
from services.medic.store.writer import usage_state
from services.medic.tests.store.chains import draft, load, seed_instance

INSTANCE = re.compile(r"^mi_[0-9a-f]{16}$")


@pytest.fixture
def loose_umask():
    """Prove the store sets its own modes instead of relying on S1's umask 077."""
    old = os.umask(0o022)
    yield
    os.umask(old)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# --- modes on a new store ----------------------------------------------------------------


def test_a_new_store_is_private(tmp_path: Path, loose_umask) -> None:
    root = tmp_path / "medic"
    with open_writer(root) as w:
        w.append(draft(load("chain-03-store-reset.jsonl")[1]) | {"body": _own(w)})
        assert _mode(root) == 0o700
        for name in ("medic.db", "medic.db-wal", "medic.db-shm", "medic.lock"):
            assert _mode(root / name) == 0o600, name
        assert _mode(root / "instance_id") == 0o600


def _own(w) -> dict:
    """The fixture's incident, re-pointed at this store's instance."""
    body = load("chain-03-store-reset.jsonl")[1]["body"]
    return body | {"instance_id": w.instance_id}


# --- looser modes are refused, with a message that names the fix ---------------------------


def test_a_world_readable_db_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "medic"
    with open_writer(root):
        pass
    (root / "medic.db").chmod(0o644)
    with pytest.raises(StoreRefused, match=r"medic\.db.*0644.*chmod 600"):
        open_writer(root)


def test_a_world_readable_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "medic"
    with open_writer(root):
        pass
    root.chmod(0o755)
    with pytest.raises(StoreRefused, match=r"0755.*chmod 700"):
        open_writer(root)


@pytest.mark.parametrize("name", ["instance_id", "medic.lock", "medic.db-wal"])
def test_other_store_files_with_loose_modes_are_refused(tmp_path: Path, name) -> None:
    root = tmp_path / "medic"
    with open_writer(root) as w:
        w.append(draft(load("chain-03-store-reset.jsonl")[1]) | {"body": _own(w)})
        keep = sqlite3.connect(root / "medic.db")  # keeps the WAL past the close
        keep.execute("SELECT count(*) FROM records").fetchone()
        (root / name).chmod(0o604)
    try:
        with pytest.raises(StoreRefused, match=name):
            open_writer(root)
    finally:
        keep.close()


def test_a_symlinked_db_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "medic"
    with open_writer(root):
        pass
    (root / "medic.db").rename(tmp_path / "elsewhere.db")
    (root / "medic.db").symlink_to(tmp_path / "elsewhere.db")
    with pytest.raises(StoreRefused, match="symlink"):
        open_writer(root)


def test_a_symlink_anywhere_under_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "medic"
    with open_writer(root):
        pass
    (root / "run").mkdir(mode=0o700)
    (root / "run" / "state").symlink_to(tmp_path)
    with pytest.raises(StoreRefused, match=r"run/state.*symlink"):
        open_writer(root)


def test_a_symlinked_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "medic").symlink_to(real)
    with pytest.raises(StoreRefused, match="symlink"):
        open_writer(tmp_path / "medic")


def _st(mode: int, uid: int = 10001, gid: int = 10001) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))


@pytest.mark.parametrize(
    ("mode", "uid", "gid", "refused"),
    [
        (stat.S_IFDIR | 0o700, 10001, 10001, False),
        (stat.S_IFDIR | 0o2770, 10001, 10001, False),  # Helm fsGroup (C4 §6.2)
        (stat.S_IFREG | 0o600, 10001, 10001, False),
        (stat.S_IFREG | 0o660, 10001, 10001, False),  # fsGroup relabel, own gid
        (stat.S_IFDIR | 0o750, 10001, 0, True),  # another group can read
        (stat.S_IFREG | 0o640, 10001, 20, True),
        (stat.S_IFDIR | 0o701, 10001, 10001, True),  # anyone
        (stat.S_IFREG | 0o604, 10001, 10001, True),
        (stat.S_IFDIR | 0o700, 0, 10001, True),  # owned by another uid
    ],
)
def test_the_mode_rule(mode: int, uid: int, gid: int, refused: bool) -> None:
    problem = mode_problem(_st(mode, uid, gid), euid=10001, egid=10001)
    assert (problem is not None) is refused, problem


# --- instance_id (X1 ⚑5a) ------------------------------------------------------------------


def test_instance_id_is_random_and_persisted_beside_the_store(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    with open_writer(a) as w:
        first = w.instance_id
    assert INSTANCE.match(first)
    assert (a / "instance_id").read_text().strip() == first
    with open_writer(a) as w:
        assert w.instance_id == first, "created once"
    with open_writer(b) as w:
        assert w.instance_id != first, "random, not derived"
    with sqlite3.connect(a / "medic.db") as conn:
        dump = "\n".join(conn.iterdump())
    assert first not in dump, "beside the store, not inside it"


def test_a_store_reset_records_the_instance_id(tmp_path: Path) -> None:
    with open_writer(tmp_path / "medic") as w:
        rec = w.reset(previous_head=None)
    assert rec["body"]["instance_id"] == w.instance_id
    assert rec["seq"] == 0 and rec["prev"] == "0" * 64
    assert verify_store(tmp_path / "medic").ok


@pytest.mark.parametrize(
    "bad", ["vigil-0.vigil.svc.cluster.local", "partner-a-prod", "mi_XYZ", ""]
)
def test_a_hostname_shaped_instance_id_is_refused(tmp_path: Path, bad: str) -> None:
    root = tmp_path / "medic"
    seed_instance(root, bad)
    with pytest.raises(StoreRefused, match="instance_id"):
        open_writer(root)


def test_a_lost_instance_id_on_a_used_store_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "medic"
    with open_writer(root) as w:
        w.reset(previous_head=None)
    (root / "instance_id").unlink()
    with pytest.raises(StoreRefused, match="instance_id"):
        open_writer(root)


# --- cap and reserve: a stub that reports (purge and enforcement are G3) ---------------------


def test_usage_reports_bytes_against_the_cap_and_free_space_against_the_reserve(
    tmp_path: Path,
) -> None:
    root = tmp_path / "medic"
    with open_writer(root) as w:
        w.reset(previous_head=None)
        u = w.usage()
        on_disk = sum(
            (root / n).stat().st_size
            for n in ("medic.db", "medic.db-wal", "medic.db-shm")
            if (root / n).exists()
        )
    assert u.cap_bytes == GIB and u.reserve_bytes == RESERVE_BYTES == 64 * 2**20
    assert u.used_bytes == on_disk > 0
    assert u.free_bytes > 0
    assert u.state == "ok"


@pytest.mark.parametrize(("cap", "state"), [(10**9, "ok"), (1, "over_cap")])
def test_usage_states(tmp_path: Path, cap: int, state: str) -> None:
    with open_writer(tmp_path / "medic", cap_bytes=cap) as w:
        assert w.usage().state == state


@pytest.mark.parametrize(
    ("used", "state"),
    [(799, "ok"), (800, "near_cap"), (1000, "near_cap"), (1001, "over_cap")],
)
def test_near_cap_from_80_percent(used: int, state: str) -> None:
    # C4 §6.4 step 1: "store near cap" at 80 % of the cap.
    assert usage_state(used, 1000) == state
