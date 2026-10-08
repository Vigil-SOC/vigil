"""The data root and the files beside the database (C4 §6.1, X1 ⚑5a).

Medic refuses to start on a data root it can't trust: owned by another uid,
readable by another group or by everyone, or with a symlink anywhere under it
(which could redirect writes into Vigil's State Directory). The image sets the
uid/gid (10001, S1); this module only checks, it never chowns.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

DB_NAME = "medic.db"
LOCK_NAME = "medic.lock"
INSTANCE_NAME = "instance_id"
SIDE_FILES = (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", LOCK_NAME, INSTANCE_NAME)

# decision-record.schema.json $defs/instance_id: random only, never a host name.
INSTANCE_RE = re.compile(r"^mi_[0-9a-f]{16}$")


class StoreError(Exception):
    """Base for every store failure."""


class StoreRefused(StoreError):
    """The store won't open: it isn't safe, or it isn't what Medic wrote."""


def is_fsgroup_root(st: os.stat_result, *, egid: int) -> bool:
    """Helm's signature (C4 §6.2): fsGroup leaves the volume root setgid, group Medic's gid."""
    return (
        stat.S_ISDIR(st.st_mode)
        and bool(st.st_mode & stat.S_ISGID)
        and st.st_gid == egid
    )


def mode_problem(
    st: os.stat_result, *, euid: int, egid: int, fsgroup: bool
) -> str | None:
    """C4 §6.1's rule (PROVISIONAL S2-1), for the root and every file in it.

    Nothing is ever allowed for "other". By default the brief's 0600/0700 holds:
    no group bits, owner = Medic. Only on an fsGroup volume (`fsgroup`: the root
    has the Helm signature) may Medic's own gid have group bits (a kubelet
    relabel adds g+rw), and may the root itself be owned by another uid (a fresh
    PVC's root is often root-owned; fsGroup changes the group, not the owner).
    Not on plain group ownership: on macOS the primary group (`staff`) is shared.
    """
    mode = stat.S_IMODE(st.st_mode)
    helm_root = fsgroup and is_fsgroup_root(st, egid=egid)
    if st.st_uid != euid and not helm_root:
        return f"owned by uid {st.st_uid}, not Medic's uid {euid}"
    if mode & 0o007:
        return f"mode {mode:04o} lets everyone in"
    if mode & 0o070 and not (fsgroup and st.st_gid == egid):
        return f"mode {mode:04o} lets group {st.st_gid} in"
    return None


def _fix(path: Path, is_dir: bool) -> str:
    return f"chmod {'700' if is_dir else '600'} {path}"


def check_private(path: Path, *, fsgroup: bool) -> None:
    """Refuse a symlink, or a mode or owner looser than mode_problem allows."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise StoreRefused(f"{path} is a symlink; Medic's data root must hold none")
    problem = mode_problem(st, euid=os.geteuid(), egid=os.getegid(), fsgroup=fsgroup)
    if problem:
        is_dir = stat.S_ISDIR(st.st_mode)
        raise StoreRefused(
            f"{path}: {problem}. Medic keeps its store private; fix with: "
            f"{_fix(path, is_dir)} (and chown to Medic's user if needed)"
        )


def prepare_root(root: Path) -> None:
    """Create the root (0700) if it's missing, then check it and everything under it."""
    if not os.path.lexists(root):
        root.mkdir(mode=0o700, parents=True)
    root_st = root.lstat()
    fsgroup = not stat.S_ISLNK(root_st.st_mode) and is_fsgroup_root(
        root_st, egid=os.getegid()
    )
    check_private(root, fsgroup=fsgroup)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            if path.is_symlink():
                rel = path.relative_to(root)
                raise StoreRefused(
                    f"{root}: {rel} is a symlink; Medic's data root must hold none"
                )
    for name in SIDE_FILES:
        path = root / name
        if os.path.lexists(path):
            check_private(path, fsgroup=fsgroup)


def create_private_file(path: Path) -> None:
    """Create an empty 0600 file, whatever the umask; never through a symlink."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)


def load_or_create_instance_id(root: Path, *, store_is_empty: bool) -> str:
    """X1 ⚑5a: `mi_` + 16 random hex, created once, kept beside the store.

    It lives outside the database so a store reset after corruption keeps it.
    A used store whose id file is gone is refused: minting a new id would give
    one store two instance ids.
    """
    path = root / INSTANCE_NAME
    if os.path.lexists(path):
        value = path.read_text().strip()
        if not INSTANCE_RE.match(value):
            raise StoreRefused(
                f"{path}: instance_id {value[:40]!r} isn't 'mi_' + 16 hex; Medic's "
                "instance id is random and never a host or cluster name (X1 5a)"
            )
        return value
    if not store_is_empty:
        raise StoreRefused(
            f"{path} is missing but {root / DB_NAME} holds records; restore the "
            "instance_id file rather than minting a second id for one store"
        )
    value = "mi_" + secrets.token_hex(8)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".instance_id.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # fails if another process won the race
    finally:
        os.unlink(tmp)
    return value
