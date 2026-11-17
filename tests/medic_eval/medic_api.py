"""Read Medic's own API through the Medic container, as survival_checks' INCIDENTS does.

The API binds on medic-private (not loopback) and wants the key in a file
(stack.medic_key_file), so the read runs inside the container: `docker compose exec`
or `kubectl exec`, whichever the stack does. Records are paged by the keyset cursor.
"""

from __future__ import annotations

import json

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
