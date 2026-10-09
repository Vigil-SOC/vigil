"""Medic on Compose, checked on the rendered config (C3 §7 checks 1 and 13, S6).

No containers run here: `docker compose config` renders what Docker would start,
and the assertions read that. The live half (checks 4, 7, 10, 11 and the
readiness incident) is test_compose_live.py.

Enabling Medic takes two switches: `--profile medic` and the overlay
`infra/docker/medic/docker-compose.medic.yml`, which declares `medic-net` and
puts the daemon and agents on it (S6-1). With the profile off and no overlay,
the rendered config must be byte-for-byte what it is without Medic (C8: off by
default, and off changes nothing).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.medic_compose.compose import (
    BASE,
    FORBIDDEN_ENV,
    HAVE_COMPOSE,
    MEDIC_NET_MEMBERS,
    MEDIC_NETWORKS,
    MEDIC_PRIVATE_MEMBERS,
    MEDIC_SERVICES,
    NOT_ON_MEDIC_NET,
    OVERLAY,
    REPO,
    env_of,
    networks_of,
    render,
    render_raw,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not HAVE_COMPOSE, reason="needs the Docker CLI with Compose v2"),
]

UIDS = {
    "medic": "10001:10001",
    "medic-gateway": "10002:10002",
    "medic-dockerproxy": "10003:10003",
}
# Every profile in the file except medic: "off" must hold whichever of them is on.
OTHER_PROFILES = ("daemon", "dev", "observability", "splunk", "kafka", "misp")


@pytest.fixture(scope="module")
def home(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("home")


@pytest.fixture(scope="module")
def on(home) -> dict:
    """Medic enabled, with the daemon too, so every medic-net member renders."""
    return render("medic", "daemon", home=home)


# --- off by default (C8) ---------------------------------------------------


def _without_medic(tmp_path: Path) -> Path:
    raw = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    for name in MEDIC_SERVICES:
        raw["services"].pop(name, None)
    raw.get("volumes", {}).pop("medic_data", None)
    for name in ("medic_viewer_password", "medic_api_key"):
        raw.get("secrets", {}).pop(name, None)
    if raw.get("secrets") == {}:
        raw.pop("secrets")
    stripped = tmp_path / "docker-compose.without-medic.yml"
    stripped.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return stripped


@pytest.mark.parametrize(
    "profiles", [(), OTHER_PROFILES], ids=["default", "all-other-profiles"]
)
def test_profile_off_renders_exactly_as_without_medic(profiles, home, tmp_path) -> None:
    with_medic = render(*profiles, home=home, overlay=False)
    without = render(*profiles, home=home, overlay=False, base=_without_medic(tmp_path))
    assert json.dumps(with_medic, sort_keys=True) == json.dumps(without, sort_keys=True)


def test_profile_off_starts_no_medic_container(home) -> None:
    cfg = render(*OTHER_PROFILES, home=home, overlay=False)
    assert not set(MEDIC_SERVICES) & set(cfg["services"])
    assert "medic_data" not in (cfg.get("volumes") or {})
    for net in MEDIC_NETWORKS:
        assert net not in (cfg.get("networks") or {})


def test_medic_services_sit_behind_the_medic_profile_only() -> None:
    raw = yaml.safe_load(BASE.read_text(encoding="utf-8"))["services"]
    for name in MEDIC_SERVICES:
        assert raw[name].get("profiles") == ["medic"], name


def test_profile_without_overlay_fails_loudly(home) -> None:
    # Half-enabled would start Medic with no route to the agents: every read a
    # DNS error, so Medic sensor-blind for the agents. Compose refuses instead.
    done = render_raw("medic", home=home, overlay=False)
    assert done.returncode != 0
    assert "medic-net" in done.stderr or "medic-private" in done.stderr


def test_main_file_declares_no_medic_network() -> None:
    # Declared only in the overlay, so the profile alone can't start Medic.
    raw = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    assert not set(MEDIC_NETWORKS) & set(raw.get("networks") or {})


def test_overlay_touches_only_medic_net() -> None:
    raw = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    assert set(raw) == {"services", "networks"}
    assert set(raw["networks"]) == set(MEDIC_NETWORKS)
    for name, spec in raw["services"].items():
        assert name in MEDIC_NET_MEMBERS - set(MEDIC_SERVICES), name
        assert set(spec) == {"networks"}, f"{name}: the overlay only adds medic-net"
        assert list(spec["networks"]) == ["medic-net"], name


# --- C3 §7 check 1 ----------------------------------------------------------


@pytest.mark.parametrize("net", MEDIC_NETWORKS)
def test_medic_networks_are_internal(on, net) -> None:
    assert on["networks"][net].get("internal") is True


@pytest.mark.parametrize(
    "net, expected",
    [("medic-net", MEDIC_NET_MEMBERS), ("medic-private", MEDIC_PRIVATE_MEMBERS)],
)
def test_medic_networks_hold_exactly_the_listed_members(on, net, expected) -> None:
    members = {
        name for name, spec in on["services"].items() if net in networks_of(spec)
    }
    assert members == expected
    for name in NOT_ON_MEDIC_NET:
        assert net not in networks_of(on["services"][name]), name


def test_members_keep_their_own_network(on) -> None:
    for name in ("soc-daemon", "agent-worker", "agent-serve"):
        assert networks_of(on["services"][name]) == {"deeptempo-network", "medic-net"}


def test_medic_side_networks(on) -> None:
    svc = on["services"]
    assert networks_of(svc["medic"]) == set(MEDIC_NETWORKS)
    # The agents can't reach Medic's Docker proxy or its Viewer session (S6-9).
    assert networks_of(svc["medic-dockerproxy"]) == {"medic-private"}
    # The gateway is the one bridge to the backend (C3 §4.6).
    assert networks_of(svc["medic-gateway"]) == {"medic-private", "deeptempo-network"}


def test_medic_mounts_only_its_data_volume(on) -> None:
    mounts = on["services"]["medic"].get("volumes") or []
    assert [(m["type"], m["source"], m["target"]) for m in mounts] == [
        ("volume", "medic_data", "/var/lib/vigil-medic")
    ]
    for name, spec in on["services"].items():
        if name != "medic":
            sources = {m.get("source") for m in spec.get("volumes") or []}
            assert "medic_data" not in sources, f"{name} mounts Medic's store"


def test_only_the_proxy_mounts_the_docker_socket_read_only(on) -> None:
    for name in MEDIC_SERVICES:
        for m in on["services"][name].get("volumes") or []:
            if "docker.sock" in str(m.get("source")):
                assert name == "medic-dockerproxy", name
                assert m["type"] == "bind" and m.get("read_only") is True
                assert m["target"] == "/var/run/docker.sock"
                # A missing socket must fail, not become an empty directory.
                assert m["bind"]["create_host_path"] is False
    sock = [m for m in on["services"]["medic-dockerproxy"].get("volumes") or []]
    assert len(sock) == 1 and "docker.sock" in sock[0]["source"]
    assert on["services"]["medic-gateway"].get("volumes") in (None, [])


def test_no_vigil_home_on_any_medic_service(on) -> None:
    for name in MEDIC_SERVICES:
        for m in on["services"][name].get("volumes") or []:
            assert m.get("source") not in ("vigil_home", "vigil_investigations"), name
            assert ".vigil" not in str(m.get("source")), name
            assert ".vigil" not in str(m.get("target")), name
    # Secrets are bind mounts too: none may come from Vigil's state directory.
    for name, secret in on["secrets"].items():
        assert "/.vigil/" not in secret["file"], name


# --- hardening (C4 uid, C7 limits, no published ports) ----------------------


@pytest.mark.parametrize("name", MEDIC_SERVICES)
def test_hardening(on, name) -> None:
    spec = on["services"][name]
    assert spec.get("user") == UIDS[name]
    assert spec.get("read_only") is True
    assert spec.get("cap_drop") == ["ALL"]
    assert not spec.get("cap_add")
    assert "no-new-privileges:true" in (spec.get("security_opt") or [])
    assert not spec.get("privileged")
    assert spec.get("pids_limit")
    for key in ("pid", "ipc", "network_mode", "userns_mode"):
        assert key not in spec, f"{name} sets {key}"


@pytest.mark.parametrize("name", MEDIC_SERVICES)
def test_no_published_ports(on, name) -> None:
    # The port-binding ratchet only sees default-profile services; Medic is
    # profiled, so its own gate is here: nothing published at all.
    assert not on["services"][name].get("ports"), name


def test_medic_resource_limits(on) -> None:
    spec = on["services"]["medic"]
    assert float(spec["cpus"]) == 1.0  # C7
    assert int(spec["mem_limit"]) == 1 << 30


def test_medic_healthcheck_is_check(on) -> None:
    hc = on["services"]["medic"]["healthcheck"]
    assert hc["test"] == ["CMD", "python", "-m", "services.medic", "check"]


def test_medic_watches_the_agent_worker_on_medic_net(on) -> None:
    env = env_of(on["services"]["medic"])
    assert env["VIGIL_MEDIC_INSTALL_SHAPE"] == "compose"
    assert env["VIGIL_MEDIC_AGENT_WORKER_ADDR"] == "agent-worker:6990"


def test_gateway_binds_each_listener_to_one_network(on) -> None:
    spec = on["services"]["medic-gateway"]
    env = env_of(spec)
    out_host = env["VIGIL_MEDIC_GATEWAY_OUT_BIND"].rpartition(":")[0]
    in_host = env["VIGIL_MEDIC_GATEWAY_IN_BIND"].rpartition(":")[0]
    nets = spec["networks"]
    # SP1 ⚑6: an alias that exists on exactly one network, so it resolves to that
    # network's address only (the service name resolves on both).
    assert out_host in (nets["medic-private"] or {}).get("aliases", [])
    assert in_host in (nets["deeptempo-network"] or {}).get("aliases", [])
    assert out_host not in (nets["deeptempo-network"] or {}).get("aliases", [])
    assert in_host not in (nets["medic-private"] or {}).get("aliases", [])
    assert env["VIGIL_MEDIC_GATEWAY_OUT_BIND"].endswith(":8471")
    assert env["VIGIL_MEDIC_GATEWAY_IN_BIND"].endswith(":8470")
    assert env["VIGIL_MEDIC_GATEWAY_BACKEND"] == "backend:6987"


def test_proxy_binds_its_medic_net_name(on) -> None:
    env = env_of(on["services"]["medic-dockerproxy"])
    assert env["VIGIL_MEDIC_DOCKERPROXY_BIND"] == "medic-dockerproxy:8472"
    assert on["services"]["medic-dockerproxy"].get("group_add")


# --- K1 §6 C6: no tokens; C3 §7 check 13: no literal Viewer password ---------


@pytest.mark.parametrize("name", MEDIC_SERVICES)
def test_no_agent_or_daemon_token_in_env(on, name) -> None:
    spec = on["services"][name]
    assert not spec.get("env_file"), f"{name} must not read an env file"
    present = set(env_of(spec)) & set(FORBIDDEN_ENV)
    assert not present, f"{name} env has {sorted(present)}"
    for key in env_of(spec):
        assert not key.endswith(("_PASSWORD", "_TOKEN", "_SECRET", "_API_KEY")), key


def test_viewer_password_arrives_as_a_file_secret(on) -> None:
    secrets = on["secrets"]
    assert [s["source"] for s in on["services"]["medic-gateway"]["secrets"]] == [
        "medic_viewer_password"
    ]
    assert [s["source"] for s in on["services"]["medic"]["secrets"]] == [
        "medic_api_key"
    ]
    assert not on["services"]["medic-dockerproxy"].get("secrets")
    for name in ("medic_viewer_password", "medic_api_key"):
        assert set(secrets[name]) == {
            "name",
            "file",
        }, f"{name}: file only, no inline value"


def test_rendered_compose_holds_no_generated_secret(tmp_path) -> None:
    import subprocess

    from tests.medic_compose.compose import ENABLE, clean_env

    secrets_dir = tmp_path / "secrets"
    env = clean_env(tmp_path, VIGIL_MEDIC_SECRETS_DIR=str(secrets_dir))
    subprocess.run(
        [str(ENABLE), "--secrets-only"], env=env, check=True, capture_output=True
    )
    cfg = json.dumps(
        render(
            "medic",
            "daemon",
            home=tmp_path,
            env={"VIGIL_MEDIC_SECRETS_DIR": str(secrets_dir)},
        )
    )
    for name in ("viewer_password", "api_key"):
        value = (secrets_dir / name).read_text().strip()
        assert value and value not in cfg


def test_env_example_has_no_viewer_password() -> None:
    text = (REPO / "env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        key, _, value = line.lstrip("# ").partition("=")
        if key.startswith("VIGIL_MEDIC") and ("PASSWORD" in key or "API_KEY" in key):
            assert not value.strip(), f"literal secret for {key} in env.example"


def test_master_flag_is_off_unless_set(home) -> None:
    # C8 rule 1: the flag is the master switch, so the profile alone (or
    # `docker compose up medic` by name) must not start a running Medic.
    unset = render("medic", home=home)["services"]["medic"]
    assert env_of(unset)["VIGIL_MEDIC_ENABLED"] == "false"
    on = render("medic", home=home, env={"VIGIL_MEDIC_ENABLED": "true"})
    assert env_of(on["services"]["medic"])["VIGIL_MEDIC_ENABLED"] == "true"


# --- V1: the backend reads the same switch (Off ≠ Down) ---------------------


@pytest.mark.parametrize("value", [None, "true", "false"])
@pytest.mark.parametrize("profiles", [(), ("medic",)])
def test_backend_reads_the_same_flag_as_medic(home, value, profiles) -> None:
    # C8: flag on without the profile must read as "down" (loud), so the
    # backend gets the flag from the main file, not from the overlay.
    env = {} if value is None else {"VIGIL_MEDIC_ENABLED": value}
    cfg = render(*profiles, home=home, overlay=bool(profiles), env=env)["services"]
    expected = value or "false"
    assert env_of(cfg["backend"])["VIGIL_MEDIC_ENABLED"] == expected
    if profiles:
        assert env_of(cfg["medic"])["VIGIL_MEDIC_ENABLED"] == expected


def test_gateway_has_no_default_viewer_name(home) -> None:
    # D2-17: unset, the gateway refuses to start rather than try a guessable name.
    unset = render("medic", home=home)["services"]["medic-gateway"]
    assert env_of(unset)["VIGIL_MEDIC_GATEWAY_VIEWER_USER"] == ""
    named = render(
        "medic", home=home, env={"VIGIL_MEDIC_VIEWER_USER": "medic-a1b2c3d4e5f6"}
    )["services"]["medic-gateway"]
    assert env_of(named)["VIGIL_MEDIC_GATEWAY_VIEWER_USER"] == "medic-a1b2c3d4e5f6"
