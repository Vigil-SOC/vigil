"""Run inside the Medic container: `docker exec -i <medic> python - '<json>' < probe.py`.

Prints one JSON object of what Medic can and can't reach. stdlib only, so it
needs nothing beyond the Medic image. The test decides pass or fail; this only
reports, so a failing check shows exactly what happened.
"""

from __future__ import annotations

import json
import socket
import sys

ARGS = json.loads(sys.argv[1])
GW, GW_IN, PROXY = (
    ("medic-gateway", 8471),
    ("medic-gateway", 8470),
    ("medic-dockerproxy", 8472),
)


def connect(host: str, port: int, timeout: float = 3.0) -> str:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return "connected"
    except socket.gaierror:
        return "dns"
    except TimeoutError:
        return "timeout"
    except OSError as e:
        return type(e).__name__


def raw(addr: tuple[str, int], request: str, read: int = 65536) -> dict:
    """Send exact bytes (a client library would normalise the bypass shapes)."""
    try:
        s = socket.create_connection(addr, timeout=10)
    except OSError as e:
        return {"status": None, "error": type(e).__name__}
    with s:
        s.sendall(request.encode("latin-1"))
        data = b""
        while len(data) < read:
            try:
                chunk = s.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            data += chunk
            head, sep, body = data.partition(b"\r\n\r\n")
            if sep:
                lengths = [
                    int(line.split(b":", 1)[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ]
                if lengths and len(body) >= lengths[0]:
                    break
    head, _, body = data.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].split()
    status = int(status_line[1]) if len(status_line) > 1 else None
    return {"status": status, "body": body.decode("utf-8", "replace")}


def get(
    addr, path: str, method: str = "GET", headers: str = "", body: str = ""
) -> dict:
    host = addr[0]
    length = f"Content-Length: {len(body)}\r\n" if body or method == "POST" else ""
    req = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n{headers}{length}Connection: close\r\n\r\n{body}"
    return raw(addr, req)


out: dict = {}

# --- C3 check 7: no route to Redis, Bifrost, Postgres or the backend; no internet.
out["by_name"] = {
    f"{h}:{p}": connect(h, p)
    for h, p in [
        ("redis", 6379),
        ("bifrost", 8080),
        ("backend", 6987),
        ("postgres", 5432),
    ]
}
out["by_ip"] = {
    f"{name}@{ip}:{port}": connect(ip, port)
    for name, ip in ARGS["deeptempo_ips"].items()
    for port in ARGS["ports"]
}
out["internet"] = {
    f"{h}:{p}": connect(h, p) for h, p in [("1.1.1.1", 443), ("8.8.8.8", 53)]
}
try:
    socket.getaddrinfo("example.com", 443)
    out["internet_dns"] = "resolved"
except OSError:
    out["internet_dns"] = "failed"
# The host side of each Medic network's bridge (its IPAM gateway): Docker
# publishes ports like the backend's 0.0.0.0:6987 on every host address, this
# one included, so an internal network must drop traffic to it too.
out["host_gateway"] = {
    f"{gw}:{port}": connect(gw, port)
    for gw in ARGS["gateways"]
    for port in ARGS["host_ports"]
}
# Members Medic must reach on its two networks.
out["members"] = {
    f"{h}:{p}": connect(h, p)
    for h, p in [
        ("agent-worker", 6990),
        ("agent-serve", 6989),
        ("soc-daemon", 9091),
        GW,
        PROXY,
    ]
}

# --- C3 check 11: no route to reset-confirm, directly or via the gateway.
confirm_body = '{"token":"x","new_password":"Aa1!aaaaaaaa"}'
ctype = "Content-Type: application/json\r\n"
out["reset_confirm_via_gateway"] = get(
    GW, "/api/auth/password-reset/confirm", "POST", ctype, confirm_body
)
out["reset_confirm_gateway_inbound_port"] = connect(*GW_IN)

# --- C3 check 10: the gateway's path list, bypass shapes included.
canary = ARGS["canary"]
bypass = {
    "dotdot": ("GET", "/api/health/../auth/me"),
    "pct2F": ("GET", "/api%2Fauth%2Fme"),
    "double_slash": ("GET", "//api/federation/sources"),
    "context_prefix": ("GET", "/vigil/api/federation/sources"),
    "auth_me": ("GET", "/api/auth/me"),
    "off_list": ("GET", "/api/users"),
    "head": ("HEAD", "/api/health"),
    "options": ("OPTIONS", "/api/health"),
    "post_findings": ("POST", "/api/v1/findings?data_source=probe"),
}
out["bypass"] = {k: get(GW, p, m)["status"] for k, (m, p) in bypass.items()}
out["bypass"]["method_override"] = get(
    GW, "/api/health", headers="X-HTTP-Method-Override: POST\r\n"
)["status"]
out["allowed"] = get(
    GW,
    "/api/federation/sources",
    headers=f"Authorization: Bearer {canary}\r\nCookie: session={canary}\r\n",
)
out["gw_status"] = get(GW, "/_gw/status")

# --- C3 check 4: the Docker proxy.
cid = ARGS["container_id"]
out["proxy"] = {
    "restart": get(PROXY, f"/containers/{cid}/restart", "POST")["status"],
    "archive": get(PROXY, f"/containers/{cid}/archive?path=/")["status"],
    "export": get(PROXY, f"/containers/{cid}/export")["status"],
    "attach_ws": get(PROXY, f"/containers/{cid}/attach/ws?stdin=1&stream=1")["status"],
    "exec": get(PROXY, f"/containers/{cid}/exec", "POST")["status"],
    "list": get(PROXY, "/containers/json")["status"],
}
inspect = get(PROXY, f"/containers/{cid}/json")
out["proxy"]["inspect"] = inspect["status"]
try:
    body = json.loads(inspect["body"])
    out["proxy"]["inspect_config_keys"] = sorted(body.get("Config", {}))
except ValueError:
    out["proxy"]["inspect_config_keys"] = None
out["proxy"]["inspect_has_token"] = ARGS["token"] in inspect["body"]

print(json.dumps(out))
