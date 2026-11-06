"""Read Medic's own API through the Medic container, as survival_checks' INCIDENTS does.

The API binds on medic-private (not loopback) and wants the key in
/run/secrets/medic_api_key, so the read runs inside the container. Records are
paged by the keyset cursor.
"""

from __future__ import annotations

import json

READ = r"""
import json, socket, sys, urllib.request
key = open("/run/secrets/medic_api_key").read().strip()
path, paged = sys.argv[1], sys.argv[2] == "paged"
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


def get(stack, path: str, paged: bool = False):
    """The parsed reply of GET `path`, or None when the API can't be reached."""
    done = stack.compose(
        "exec", "-T", "medic", "python", "-c", READ, path, "paged" if paged else "one", check=False
    )
    return json.loads(done.stdout.strip() or "null")


def status(stack) -> dict | None:
    return get(stack, "/v1/status")


def decisions(stack) -> list[dict] | None:
    return get(stack, "/v1/decisions?limit=100", paged=True)


def sensors(stack) -> dict[str, dict]:
    page = get(stack, "/v1/sensors") or {"items": []}
    return {s["id"]: s for s in page["items"]}
