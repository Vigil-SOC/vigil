"""The startup refusal's marker (#2082): when Medic first refused to run because
NetworkPolicy wasn't enforced. A refusal exits before the store opens, so this
file is how a later clean start knows the cluster was unprotected for a while."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# A refusal shorter than this is a plugin still coming up, not a finding.
MIN_AGE_S = 120


def _path(data_dir: Path) -> Path:
    return data_dir / "run" / "policy-refused"


def read(data_dir: Path) -> float | None:
    """The first refusal's wall time, or None if there is no usable marker."""
    try:
        return float(_path(data_dir).read_text())
    except (OSError, ValueError):
        return None


def record(data_dir: Path, now: float) -> None:
    """Keep the first refusal of a streak: a repeat refusal doesn't move the time."""
    if read(data_dir) is not None:
        return
    path = _path(data_dir)
    # The data dir first, so `parents=True` doesn't create it at the umask's mode.
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".policy-refused.")  # 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(repr(now))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def clear(data_dir: Path) -> None:
    _path(data_dir).unlink(missing_ok=True)
