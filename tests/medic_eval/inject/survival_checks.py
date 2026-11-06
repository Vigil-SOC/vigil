"""Medic keeps running through dependency outages and Viewer revocation (#3740).

Plain pass/fail checks beside the cells, not Cells: no labels.json. "Still
running" is the heartbeat file's `cycle` (services/medic/app/heartbeat.py), which
Medic writes every pass; decision records are written on events, not on a clock.
Incidents Medic opens during an outage are listed in the result and not scored.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

from services.medic.app.heartbeat import STALE_AFTER_S
from tests.medic_eval.inject.stack import Stack

# Longer than STALE_AFTER_S, so a dead Medic would show as a stale heartbeat.
WINDOW_S = STALE_AFTER_S + 15
SAMPLE_S = 45  # beats come at least every 30 s
DATA_DIR = "/var/lib/vigil-medic"

HEARTBEAT = f"import json; print(json.load(open('{DATA_DIR}/run/heartbeat'))['cycle'])"
# One GET or other method from the Medic container (medic-private) to the gateway.
GATEWAY = r"""
import json, sys, urllib.request, urllib.error
method, path = sys.argv[1:3]
req = urllib.request.Request("http://medic-gateway-out:8471" + path, method=method,
                             data=b"{}" if method != "GET" else None,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        out = [r.status, r.read().decode()]
except urllib.error.HTTPError as e:
    out = [e.code, e.read().decode()]
except Exception as e:
    out = [0, str(e)]
print(json.dumps(out))
"""
# Medic's own API on its medic-private address (it binds there, not loopback).
INCIDENTS = r"""
import json, socket, urllib.request
key = open("/run/secrets/medic_api_key").read().strip()
for addr in sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), 8470)}):
    try:
        req = urllib.request.Request(f"http://{addr}:8470/v1/incidents?state=all&limit=200",
                                     headers={"X-Medic-Key": key})
        with urllib.request.urlopen(req, timeout=15) as r:
            items = json.load(r)["items"]
        print(json.dumps([[i["incident_id"], (i.get("rule") or {}).get("id"), i.get("state")] for i in items]))
        break
    except Exception:
        continue
else:
    print("null")
"""
# What an admin's Disable plus the sign-out-everywhere the backend does on a role or
# email change or a delete: the account is off and its open tokens end (Redis cutoff).
SIGN_OUT = (
    "import asyncio, sys; from core.auth.token_blacklist import revoke_all_for_user; "
    "asyncio.run(revoke_all_for_user(sys.argv[1]))"
)
# Refused whatever the Viewer session's state: every write, and reads off the list.
REFUSED = (
    ("POST", "/api/federation/sources"),
    ("PUT", "/api/federation/settings"),
    ("PATCH", "/api/triage"),
    ("DELETE", "/api/federation/sources"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/users"),
    ("GET", "/api/v1/findings"),
)


@dataclass
class Outage:
    service: str
    before: dict
    samples: list[dict | None]
    after: dict | None
    incidents: list = field(default_factory=list)
    restored: list[str] = field(default_factory=list)


def medic_id(s: Stack) -> str:
    return s.compose("ps", "-aq", "medic").stdout.strip()


def snapshot(s: Stack) -> dict | None:
    """The Medic container's StartedAt and RestartCount, and the heartbeat's cycle.

    None when Medic can't be asked (stopped, or no heartbeat to read)."""
    done = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}} {{.RestartCount}} {{.State.Running}}", medic_id(s)],
        capture_output=True, text=True, check=False,
    )
    beat = s.compose("exec", "-T", "medic", "python", "-c", HEARTBEAT, check=False)
    if done.returncode or beat.returncode:
        return None
    started, restarts, running = done.stdout.split()
    return {"started_at": started, "restarts": int(restarts), "running": running == "true", "cycle": int(beat.stdout)}


def gateway(s: Stack, method: str, path: str) -> tuple[int, str]:
    return tuple(json.loads(s.py("medic", GATEWAY, method, path)))


def incidents(s: Stack) -> list:
    """Every incident Medic holds, or None if its API can't be reached from here."""
    done = s.compose("exec", "-T", "medic", "python", "-c", INCIDENTS, check=False)
    return json.loads(done.stdout.strip() or "null")


def healthy(s: Stack, service: str) -> bool:
    return '"healthy"' in s.compose("ps", "-a", "--format", "json", service).stdout


def up(s: Stack, service: str) -> bool:
    """Running, and healthy if it has a healthcheck."""
    out = s.compose("ps", "--format", "json", service, check=False).stdout
    return '"State":"running"' in out and ('"Health":""' in out or '"healthy"' in out)


def restore(s: Stack, down_before: set[str]) -> list[str]:
    """Start the Vigil services that went down during an outage, as the shipped
    `unless-stopped` policy would (compose.eval.yml sets `restart: "no"`). Ones
    down before it aren't the outage's doing: vigil_harm reports them."""
    newly = [svc for svc in s.not_running() if svc not in down_before]
    if newly:
        s.compose("start", *newly, check=False)
        for svc in newly:
            try:
                s.wait(lambda: up(s, svc), bool, timeout=240)
            except AssertionError:
                raise AssertionError(f"{svc} went down during the outage and didn't come back") from None
    return newly


def recovered(s: Stack, service: str) -> None:
    """The dependency, the backend and Medic are all healthy again."""
    s.wait(lambda: healthy(s, service), bool, timeout=240)
    s.wait(lambda: s.http("GET", "backend", 6987, "/api/health")[0], lambda c: c == 200)
    s.wait(lambda: healthy(s, "medic"), bool, timeout=420)
    s.wait(
        lambda: s.compose("exec", "-T", "medic", "python", "-m", "services.medic", "check", check=False).returncode,
        lambda rc: rc == 0,
    )


def watch(s: Stack) -> list[dict | None]:
    """Heartbeat samples across the window, one every SAMPLE_S."""
    samples = []
    end = time.monotonic() + WINDOW_S
    while time.monotonic() < end:
        time.sleep(min(SAMPLE_S, max(end - time.monotonic(), 1)))
        samples.append(snapshot(s))
    return samples


def next_beat(s: Stack, last: int) -> dict | None:
    """One more beat after the window; a stopped Medic never gives it."""
    try:
        return s.wait(lambda: snapshot(s), lambda a: a and a["cycle"] > last, timeout=90)
    except AssertionError:
        return snapshot(s)


def run_outage(s: Stack, service: str) -> Outage:
    """Stop `service` for the window, start it, wait until it and Medic are healthy."""
    before, held = snapshot(s), incidents(s)
    assert before, "Medic isn't running before the outage"
    down_before = set(s.not_running())
    s.compose("stop", service)
    try:
        samples = watch(s)
    finally:
        s.compose("start", service)
    recovered(s, service)
    restored = restore(s, down_before)
    after = next_beat(s, (samples[-1] or before)["cycle"])
    opened = None if held is None else [i for i in incidents(s) or [] if i not in held]
    return Outage(service, before, samples, after, opened, restored)


def assert_alive(o: Outage) -> None:
    """The liveness predicate: cycle strictly advances through the window; Medic is never restarted."""
    seen = [o.before["cycle"]]
    for i, sample in enumerate(o.samples):
        assert sample, f"liveness: Medic didn't answer {(i + 1) * SAMPLE_S} s into the {o.service} outage"
        assert sample["running"], f"liveness: Medic container stopped during the {o.service} outage"
        assert sample["cycle"] > seen[-1], f"liveness: heartbeat cycle stuck at {seen[-1]} during the {o.service} outage"
        seen.append(sample["cycle"])
    assert o.after, f"liveness: Medic didn't answer after the {o.service} outage"
    for key in ("started_at", "restarts"):
        assert o.after[key] == o.before[key], f"liveness: Medic's {key} changed ({o.before[key]} -> {o.after[key]})"
    assert o.after["cycle"] > seen[-1], "liveness: heartbeat didn't advance after the outage"


def check_dependency_down(s: Stack, service: str) -> dict:
    o = run_outage(s, service)
    assert_alive(o)
    return {"service": service, "cycles": [o.before["cycle"], *(x["cycle"] for x in o.samples), o.after["cycle"]],
            "incidents_opened": o.incidents, "restored": o.restored}


def check_viewer_revoked(s: Stack) -> dict:
    """Deactivate the gateway's Viewer and end its tokens: Medic keeps running, the gateway says why, no write path appears."""
    name = (s.secrets_dir / "viewer_username").read_text().strip()
    refused = lambda: [gateway(s, m, p)[0] for m, p in REFUSED]  # noqa: E731
    state = lambda: json.loads(gateway(s, "GET", "/_gw/status")[1])["state"]  # noqa: E731
    before, held, probes = snapshot(s), incidents(s), refused()
    assert before and state() == "ok", "the gateway's session isn't ok before the revocation"
    assert all(c and not 200 <= c < 300 for c in probes), f"a non-read path answered: {probes}"
    s.sql(f"UPDATE users SET is_active = false WHERE username = '{name}'")
    try:
        # Off alone the backend answers a live token 403 ("inactive"), which the
        # gateway doesn't take for a session failure; ending the token makes the
        # next read a 401, then a refused refresh and login.
        s.py("backend", SIGN_OUT, s.sql(f"SELECT user_id FROM users WHERE username = '{name}'"))
        reported = s.wait(state, lambda v: v != "ok", timeout=180)
        samples = watch(s)
        still = state()
        after = refused()
    finally:
        s.sql(f"UPDATE users SET is_active = true, failed_login_count = 0 WHERE username = '{name}'")
        # The gateway stops for good after two 401s until the password changes.
        s.compose("restart", "medic-gateway")
    # Back to a clean stack before anything is asserted.
    s.wait(state, lambda v: v == "ok", timeout=180)
    recovered(s, "medic-gateway")
    o = Outage("viewer", before, samples, next_beat(s, (samples[-1] or before)["cycle"]))
    assert_alive(o)
    assert still != "ok", "the gateway's session recovered while the account was off"
    assert after == probes, f"the gateway's refusals changed: {probes} -> {after}"
    opened = None if held is None else [i for i in incidents(s) or [] if i not in held]
    return {"service": "viewer", "gateway_state": reported, "refusals": after, "incidents_opened": opened,
            "cycles": [o.before["cycle"], *(x["cycle"] for x in o.samples), o.after["cycle"]]}
