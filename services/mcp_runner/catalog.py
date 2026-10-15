"""Launch resolution and per-server identities for the runner's catalog.

``command``/``args``/``cwd``/``env`` come from the runner's own
``mcp-config.json``, read raw: ``MCPService.servers`` has already substituted
``${VAR}`` against this process's environment, which here holds the runner's
token and none of the server's credentials. MCPService supplies only the
interpreter, the substitution and the baked-launch rewrite.

Run as ``python -m services.mcp_runner.catalog <state-dir>`` at image build to
create the per-server ``HOME`` dirs and the name→uid map beside them, so the
supervisor needs no ``CHOWN`` capability at runtime.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

import core.integrations.mcp.service as _service
from core.integrations.mcp.child_env import ca_bundle_env
from core.integrations.mcp.packaged import installed_launch
from core.integrations.mcp.service import MCPService

UID_BASE = 10001
UID_MAP = "uids.json"
# Each server's TMPDIR, inside its HOME: a shared /tmp lets servers plant
# files for each other.
TMP_DIR = "tmp"


@contextmanager
def _no_secret_fallback():
    """Keep ``_substitute_env_vars`` to the env it is given.

    Its fallback, ``get_secret``, would read this process's own environment
    (the runner's token) and probe a secret store the runner must not have.
    """
    saved = _service.get_secret
    _service.get_secret = lambda key, default=None: None
    try:
        yield
    finally:
        _service.get_secret = saved


@dataclass(frozen=True)
class Launch:
    command: str
    args: List[str]
    cwd: str
    env: Dict[str, str]


def read_entries(root: Path) -> Dict[str, dict]:
    raw = json.loads((Path(root) / "mcp-config.json").read_text())["mcpServers"]
    # String values are the file's _comment_* section headers.
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


class Catalog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.entries = read_entries(self.root)
        stub = SimpleNamespace(
            derive_remote_mcp_env=lambda: {}, get_mcp_env_vars=lambda: {}
        )
        with _no_secret_fallback():
            self._service = MCPService(
                project_root=self.root, integration_bridge=stub, detection_rules=stub
            )

    def __contains__(self, name: str) -> bool:
        return name in self.entries

    def names(self) -> List[str]:
        return sorted(self.entries)

    def launch(self, name: str, request_env: Dict[str, str], home: str) -> Launch:
        entry = self.entries[name]
        root = str(self.root)
        # The declared env may build on ${HOME}; that is the server's own HOME.
        source = {**request_env, "HOME": home}

        def sub(value: str) -> str:
            with _no_secret_fallback():
                return self._service._substitute_env_vars(value, source)

        command = entry.get("command", "python")
        if command in ("python", "python3"):
            command = str(self._service.python_exe)
        cwd = entry.get("cwd", root).replace("${workspaceFolder}", root)
        command, args = installed_launch(
            command, [sub(str(a)) for a in entry.get("args") or []]
        )
        env = {
            k: sub(str(v))
            for k, v in (entry.get("env") or {}).items()
            if not k.startswith("_")
        }
        # What the backend sent wins over the catalog's defaults.
        env.update(request_env)
        env.update(ca_bundle_env())
        env.update(HOME=home, PYTHONPATH=root, TMPDIR=str(Path(home) / TMP_DIR))
        return Launch(command, args, cwd, env)


def assign_uids(names: List[str]) -> Dict[str, int]:
    # By sorted position, so a new entry shifts every uid after it. Safe while
    # the HOMEs are baked into the image; not if they move to a volume.
    return {name: UID_BASE + i for i, name in enumerate(sorted(names))}


def load_uids(state_dir: Path) -> Dict[str, int]:
    path = Path(state_dir) / UID_MAP
    return json.loads(path.read_text()) if path.exists() else {}


def provision(state_dir: Path, names: List[str]) -> Dict[str, int]:
    """One 0700 HOME per server, owned by its uid, and the map beside them."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o755)
    uids = assign_uids(names)
    for name, uid in uids.items():
        home = state_dir / name
        for path in (home, home / TMP_DIR):
            path.mkdir(exist_ok=True)
            os.chown(path, uid, uid)
            path.chmod(0o700)
    (state_dir / UID_MAP).write_text(json.dumps(uids, indent=2, sort_keys=True))
    return uids


if __name__ == "__main__":
    names = sorted(read_entries(Path(__file__).resolve().parents[2]))
    print(json.dumps(provision(Path(sys.argv[1]), names), sort_keys=True))
