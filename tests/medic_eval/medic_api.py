"""Read Medic's own API through the Medic container, as survival_checks' INCIDENTS does.

The API binds on medic-private (not loopback) and wants the key in a file
(stack.medic_key_file), so the read runs inside the container: `docker compose exec`
or `kubectl exec`, whichever the stack does. Records are paged by the keyset cursor.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

READ = r"""
import json, socket, sys, urllib.request
path, paged, key_file = sys.argv[1], sys.argv[2] == "paged", sys.argv[3]
key = open(key_file).read().strip()
def get(addr, p):
    req = urllib.request.Request(f"http://{addr}:8470{p}", headers={"X-Medic-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)
for addr in sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), 8470)}):
    try:
        if not paged:
            print(json.dumps(get(addr, path)))
            break
        items, cursor = [], None
        while True:
            page = get(addr, path + ("&cursor=" + cursor if cursor else ""))
            items += page["items"]
            cursor = page["next_cursor"]
            if not cursor:
                break
        print(json.dumps(items))
        break
    except Exception:
        continue
else:
    print("null")
"""


POST = r"""
import json, socket, sys, urllib.error, urllib.request
path, body, key_file = sys.argv[1], sys.argv[2], sys.argv[3]
key = open(key_file).read().strip()
data = body.encode()
def send(addr):
    req = urllib.request.Request(
        f"http://{addr}:8470{path}", data=data, method="POST",
        headers={
            "X-Medic-Key": key,
            "Content-Type": "application/json",
            "X-Medic-Admin": "user:eval",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
for addr in sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), 8470)}):
    try:
        print(json.dumps(send(addr)))
        break
    except Exception:
        continue
else:
    print("null")
"""


def post(stack, path: str, body: str = "{}") -> tuple[int, str]:
    """(status, body) of POST `path` inside the Medic container."""
    out = stack.medic_exec(
        "python", "-c", POST, path, body, stack.medic_key_file
    )
    got = json.loads(out.strip() or "null")
    if got is None:
        return 0, ""
    return got


def get(stack, path: str, paged: bool = False):
    """The parsed reply of GET `path`, or None when the API can't be reached."""
    out = stack.medic_exec("python", "-c", READ, path, "paged" if paged else "one", stack.medic_key_file)
    return json.loads(out.strip() or "null")


def status(stack) -> dict | None:
    return get(stack, "/v1/status")


def decisions(stack) -> list[dict] | None:
    return get(stack, "/v1/decisions?limit=100", paged=True)


def sensors(stack) -> dict[str, dict]:
    page = get(stack, "/v1/sensors") or {"items": []}
    return {s["id"]: s for s in page["items"]}



# Built from the image's own source tree, so the stack runs the rules this checkout
# ships, not the release's bundled snapshot. It runs inside the container: the pack
# is too big for an argv and the API is only reachable from there.
IMPORT_SOURCE = r"""
import hashlib, json, socket, sys, urllib.error, urllib.request
from pathlib import Path
from services.medic.contracts.pack_build import build
key = open(sys.argv[1]).read().strip()
data = build(Path("/app/services/medic/packs/medic-core"))
def send(addr, path, body=None):
    req = urllib.request.Request(
        f"http://{addr}:8470{path}", data=body, method="POST" if body is not None else "GET",
        headers={"X-Medic-Key": key, "Content-Type": "application/json", "X-Medic-Admin": "user:eval"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
for addr in sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), 8470)}):
    try:
        print(json.dumps(send(addr, "/v1/pack/import?sha256=" + hashlib.sha256(data).hexdigest(), data)))
        break
    except Exception:
        continue
else:
    print("null")
"""


def import_source_pack(stack) -> None:
    """Import packs/medic-core as built from source, and wait until it is the active pack."""
    src = Path(__file__).parents[2] / "services/medic/packs/medic-core/pack.yaml"
    want = re.search(r"^  version: (\S+)", src.read_text(), re.M).group(1)
    if ((status(stack) or {}).get("pack") or {}).get("version") != want:
        out = stack.medic_exec("python", "-c", IMPORT_SOURCE, stack.medic_key_file)
        got = json.loads(out.strip() or "null")
        assert got and got[0] == 200, f"source pack import answered {got}"
    # The status reads the pack the loop swapped in at its last cycle.
    stack.wait(
        lambda: ((status(stack) or {}).get("pack") or {}).get("version"),
        lambda v: v == want,
        timeout=120,
    )
