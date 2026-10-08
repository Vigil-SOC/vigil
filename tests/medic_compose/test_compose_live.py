"""Medic on a running Compose stack: C3 §7 checks 4, 7, 10, 11 and the S6 goal.

Opt-in (it builds three images and runs a stack for ~5 minutes):

    VIGIL_MEDIC_COMPOSE_LIVE=1 python -m pytest tests/medic_compose -p no:cacheprovider

It runs `scripts/medic/enable-compose.sh` for real (secrets, image build, file
ownership), then brings up the real Compose files plus the Medic overlay plus
`compose.test.yml` under its own project (`medic-s6`, or
VIGIL_MEDIC_COMPOSE_PROJECT). Backend, agents, daemon, Redis, Postgres and
Bifrost are stubs on their real networks and ports; the Medic services are the
real images. Every probe runs from inside the Medic container. The project is
torn down at the end, and nothing outside it is touched.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import pytest

from tests.medic_compose.compose import (
    ENABLE,
    HAVE_COMPOSE,
    MEDIC_NET_MEMBERS,
    clean_env,
    compose_cmd,
)

HERE = Path(__file__).resolve().parent
PROJECT = os.environ.get("VIGIL_MEDIC_COMPOSE_PROJECT", "medic-s6")
SERVICES = (
    "medic",
    "medic-gateway",
    "medic-dockerproxy",
    "backend",
    "agent-worker",
    "agent-serve",
    "soc-daemon",
    "redis",
    "postgres",
    "bifrost",
)
RULE = "pipeline.agent-worker-not-ready"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("VIGIL_MEDIC_COMPOSE_LIVE") != "1",
        reason="live Compose test: set VIGIL_MEDIC_COMPOSE_LIVE=1",
    ),
    pytest.mark.skipif(not HAVE_COMPOSE, reason="needs Docker with Compose v2"),
]


class Stack:
    def __init__(self, home: Path) -> None:
        self.canary = "S6CANARY" + secrets.token_hex(8)
        self.token = "S6TOKEN" + secrets.token_hex(8)
        self.secrets_dir = home / "medic-secrets"
        self.env = clean_env(
            home,
            VIGIL_MEDIC_SECRETS_DIR=str(self.secrets_dir),
            MEDIC_S6_STUB_DIR=str(HERE),
            MEDIC_S6_CANARY=self.canary,
            AGENT_INTERNAL_TOKEN=self.token,
            VIGIL_MEDIC_DOCKER_GID=os.environ.get("VIGIL_MEDIC_DOCKER_GID", ""),
        )
        if not self.env["VIGIL_MEDIC_DOCKER_GID"]:
            del self.env["VIGIL_MEDIC_DOCKER_GID"]
        self.base = [
            *compose_cmd("medic", "daemon", files=(HERE / "compose.test.yml",)),
            "-p",
            PROJECT,
        ]

    def compose(self, *args: str, check: bool = True, timeout: float = 900):
        return subprocess.run(
            [*self.base, *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def cid(self, service: str) -> str:
        return self.compose("ps", "-q", service).stdout.strip()

    def docker(self, *args: str, input: str | None = None, check: bool = True):
        return subprocess.run(
            ["docker", *args],
            input=input,
            capture_output=True,
            text=True,
            check=check,
            env=self.env,
            timeout=120,
        )

    def medic_python(self, code: str) -> str:
        return self.docker(
            "exec", "-i", self.cid("medic"), "python", "-", input=code
        ).stdout

    def health(self, service: str) -> str:
        return self.docker(
            "inspect", "-f", "{{.State.Health.Status}}", self.cid(service)
        ).stdout.strip()


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("home"))
    enable = subprocess.run(
        [str(ENABLE)], env=s.env, capture_output=True, text=True, timeout=1200
    )
    assert enable.returncode == 0, enable.stdout + enable.stderr
    s.enable_output = enable.stdout
    s.compose("down", "-v", "--remove-orphans", check=False)
    try:
        s.compose("up", "-d", "--no-build", *SERVICES)
        s.started = time.monotonic()
        yield s
    finally:
        logs = s.compose("logs", "--no-color", check=False).stdout
        (tmp_path_factory.getbasetemp() / "medic-s6-compose.log").write_text(logs)
        s.compose("down", "-v", "--remove-orphans", check=False)


def _wait(what: str, fn, timeout: float, every: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(every)


@pytest.fixture(scope="module")
def healthy(stack):
    """All three Medic services healthy; Medic within 2 minutes of `up` (S6)."""
    _wait("medic healthy", lambda: stack.health("medic") == "healthy", 120)
    stack.medic_healthy_after = time.monotonic() - stack.started
    for name in ("medic-gateway", "medic-dockerproxy"):
        _wait(f"{name} healthy", lambda n=name: stack.health(n) == "healthy", 90)
    return stack


@pytest.fixture(scope="module")
def probe(healthy):
    s = healthy
    nets = json.loads(s.docker("inspect", s.cid("backend"), s.cid("redis")).stdout)
    ips = {}
    for c in nets + json.loads(
        s.docker("inspect", s.cid("bifrost"), s.cid("postgres")).stdout
    ):
        name = c["Config"]["Labels"]["com.docker.compose.service"]
        for net, info in c["NetworkSettings"]["Networks"].items():
            if net.endswith("deeptempo-network"):
                ips[name] = info["IPAddress"]
    assert set(ips) == {"backend", "redis", "bifrost", "postgres"}
    args = {
        "deeptempo_ips": ips,
        "ports": [6987, 6379, 8080, 5432, 22, 80, 443],
        "canary": s.canary,
        "token": s.token,
        "container_id": s.cid("agent-worker"),
    }
    # Let the gateway log in before probing the allowed path.
    _wait(
        "gateway login",
        lambda: '"ok"' in s.medic_python(_GW_STATUS),
        60,
    )
    out = s.docker(
        "exec",
        "-i",
        s.cid("medic"),
        "python",
        "-",
        json.dumps(args),
        input=(HERE / "probe.py").read_text(),
    ).stdout
    return json.loads(out)


_GW_STATUS = """
import http.client
c = http.client.HTTPConnection("medic-gateway", 8471, timeout=5)
c.request("GET", "/_gw/status")
print(c.getresponse().read().decode())
"""


def backend_seen(s: Stack) -> list[dict]:
    out = s.docker(
        "exec",
        s.cid("backend"),
        "python",
        "-c",
        "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:6987/__seen').read().decode())",
    ).stdout
    return json.loads(out)["seen"]


# --- the stack itself -------------------------------------------------------


def test_enable_prints_the_manual_step(healthy) -> None:
    assert "Viewer" in healthy.enable_output
    assert "--profile medic" in healthy.enable_output


def test_medic_healthy_within_two_minutes(healthy) -> None:
    assert healthy.medic_healthy_after <= 120


def test_medic_net_is_internal_and_holds_the_members(healthy) -> None:
    s = healthy
    net = json.loads(s.docker("network", "inspect", f"{PROJECT}_medic-net").stdout)[0]
    assert net["Internal"] is True
    members = {
        json.loads(s.docker("inspect", cid).stdout)[0]["Config"]["Labels"][
            "com.docker.compose.service"
        ]
        for cid in net["Containers"]
    }
    assert members == MEDIC_NET_MEMBERS


def test_medic_runs_as_10001_read_only(healthy) -> None:
    info = json.loads(healthy.docker("inspect", healthy.cid("medic")).stdout)[0]
    assert info["Config"]["User"] == "10001:10001"
    assert info["HostConfig"]["ReadonlyRootfs"] is True
    assert info["HostConfig"]["CapDrop"] == ["ALL"]
    assert info["HostConfig"]["NanoCpus"] == 1_000_000_000
    assert info["HostConfig"]["Memory"] == 1 << 30
    assert not info["NetworkSettings"]["Ports"]


# --- C3 check 7 -------------------------------------------------------------


def test_check7_no_route_to_backend_redis_bifrost_postgres(probe) -> None:
    assert all(v == "dns" for v in probe["by_name"].values()), probe["by_name"]
    reached = {k: v for k, v in probe["by_ip"].items() if v == "connected"}
    assert not reached, reached


def test_check7_no_internet(probe) -> None:
    assert all(v != "connected" for v in probe["internet"].values()), probe["internet"]


def test_medic_reaches_its_members(probe) -> None:
    assert all(v == "connected" for v in probe["members"].values()), probe["members"]


# --- C3 check 11 ------------------------------------------------------------


def test_check11_no_route_to_reset_confirm(probe, healthy) -> None:
    assert probe["reset_confirm_via_gateway"]["status"] in (403, 405)
    assert probe["reset_confirm_gateway_inbound_port"] != "connected"
    assert probe["by_name"]["backend:6987"] == "dns"
    assert not [r for r in backend_seen(healthy) if "password-reset" in r["path"]]


# --- C3 check 10 ------------------------------------------------------------


def test_check10_gateway_refuses_every_bypass_shape(probe, healthy) -> None:
    assert all(code in (400, 403, 405) for code in probe["bypass"].values()), probe[
        "bypass"
    ]
    paths = {(r["method"], r["path"].split("?")[0]) for r in backend_seen(healthy)}
    # Only the gateway's own login (and refresh) and the one allowed read got through.
    assert paths <= {
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/refresh"),
        ("GET", "/api/federation/sources"),
    }, paths


def test_check10_client_auth_headers_are_stripped(probe, healthy) -> None:
    assert probe["allowed"]["status"] == 200
    reads = [
        r
        for r in backend_seen(healthy)
        if r["path"].startswith("/api/federation/sources")
    ]
    assert reads
    for r in reads:
        headers = {k.lower(): v for k, v in r["headers"].items()}
        assert healthy.canary not in json.dumps(headers)
        assert "cookie" not in headers
        assert headers["authorization"].startswith("Bearer eyJ")  # the gateway's own


# --- C3 check 4 -------------------------------------------------------------


def test_check4_docker_proxy(probe) -> None:
    p = probe["proxy"]
    assert p["restart"] == 405 and p["exec"] == 405
    assert p["archive"] == 403 and p["export"] == 403 and p["attach_ws"] == 403
    assert p["list"] == 200 and p["inspect"] == 200
    assert "Env" not in p["inspect_config_keys"]
    assert p["inspect_has_token"] is False


# --- the S6 goal: a /readyz fault becomes a chained, redacted incident ------

_INCIDENTS = """
import json
from services.medic.store import open_reader
from services.medic.app import config
import os, sys
with open_reader(config.data_dir(os.environ, sys.platform)) as r:
    rows = [json.loads(x) for (x,) in r.execute("SELECT record FROM records ORDER BY seq")]
print(json.dumps([x for x in rows if x.get("type") == "incident_opened"]))
"""

_SCAN = """
import os, sys
canary = sys.argv[1].encode()
hits = []
for root, _, files in os.walk("/var/lib/vigil-medic"):
    for f in files:
        p = os.path.join(root, f)
        with open(p, "rb") as fh:
            if canary in fh.read():
                hits.append(p)
print(hits)
"""


def test_readyz_fault_opens_a_chained_redacted_incident(probe, healthy) -> None:
    s = healthy
    s.docker("exec", s.cid("agent-worker"), "touch", "/tmp/not-ready")
    flipped = time.monotonic()
    opened = _wait(
        "incident_opened",
        lambda: [
            i
            for i in json.loads(s.medic_python(_INCIDENTS))
            if i["body"]["rule"]["id"] == RULE
        ],
        360,
        every=15,
    )
    # The rule holds for 2 minutes before it opens anything.
    assert time.monotonic() - flipped >= 120
    assert len(opened) == 1
    assert opened[0]["body"]["lane"] == {"value": 3, "reason": "rule"}
    assert opened[0]["body"]["install_shape"] == "compose"
    verify = s.docker(
        "exec", s.cid("medic"), "python", "-m", "services.medic.store", "verify"
    )
    assert verify.returncode == 0, verify.stdout
    scan = s.docker(
        "exec", "-i", s.cid("medic"), "python", "-", s.canary, input=_SCAN
    ).stdout.strip()
    assert scan == "[]", scan
    logs = (
        s.docker("logs", s.cid("medic")).stdout
        + s.docker("logs", s.cid("medic")).stderr
    )
    assert s.canary not in logs
    gw_logs = s.docker("logs", s.cid("medic-gateway"))
    assert s.canary not in gw_logs.stdout + gw_logs.stderr
