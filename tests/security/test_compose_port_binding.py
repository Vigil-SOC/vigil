"""Port inventory: nothing Compose starts by default may publish on a
non-loopback interface unless that exact port has a declared off-host consumer.

Locks in the contract from issue #587. ``infra/docker/docker-compose.yml``
published Postgres, Redis and Bifrost with bare ``"5432:5432"``-style mappings,
and Docker reads a two-part mapping as ``0.0.0.0:<host>:<container>`` — so all
three were reachable from any host on the operator's network. Postgres holds
``findings``, ``cases`` and ``llm_provider_configs`` behind a password whose
default is committed to this repo; Redis runs with no ``requirepass`` at all;
Bifrost serves an unauthenticated config API that can rewrite provider
credentials.

None of them has a consumer outside the host: the backend, daemon and workers
reach them by service name over the ``deeptempo-network`` bridge, and a
host-run backend (``start.sh``) reaches them over loopback.

This is a gate rather than a one-time fix. The original exposure survived
review precisely because a bare ``"5432:5432"`` looks deliberate, so the next
service added would repeat it silently.

The allowlist is keyed by ``(service, host port)`` rather than by service name.
Exempting a whole service would mean a debug database port added to ``backend``
later rides in unnoticed — the allowlist is meant to say "this *port* is
published on purpose", not "stop checking this service".

Scope: services that start on a plain ``docker compose up``, plus every
service named in ``SHARED_HOST_PORTS`` even when profiled (``soc-daemon``, #1683).
Anything else behind a ``profiles:`` key (``pgadmin`` on ``dev``, the ``observability``
stack, ``splunk``, ``kafka``) is opt-in developer tooling, and several of those
still publish on all interfaces. ``pgadmin`` was the sharpest of them and is
fixed (#707, gated by ``test_pgadmin_exposure.py``); widening this file to the
rest means allowlisting the observability and Splunk/Kafka ports first, which is
its own change. One further gap worth knowing about:
``clients/desktop/standalone/docker-compose.yml`` ships to end users and is not
gated here (it is clean today). A service using ``network_mode: host`` publishes
everything while declaring no ``ports:`` — that one is caught explicitly below.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

# Resolved from this file, deliberately without importing application code.
# tests/security/<this file> -> repo root. A gate that an unrelated import error
# can turn into a collection error is not a gate, and the sibling files here that
# import services.api.main are one broken dependency away from that.
REPO = Path(__file__).resolve().parents[2]
COMPOSE_PATH = Path("infra/docker/docker-compose.yml")

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# Host ports with a genuine off-host consumer, keyed per port. See the module
# docstring on why this is not keyed per service.
SHARED_HOST_PORTS: dict[str, frozenset[str]] = {
    # Analyst browsers reach the REST API and the bundled SPA here.
    "backend": frozenset({"6987"}),
    # 8081 is the SIEM webhook receiver and fails closed without
    # DAEMON_WEBHOOK_TOKEN (services/daemon/poller.py).
    #
    # 9090 stays published (#587 left it alone; Prometheus scrapes it over the
    # compose bridge anyway). 9091 serves /health and /status unauthenticated,
    # so it is loopback-only (#1683) and deliberately absent from this list.
    "soc-daemon": frozenset({"8081", "9090"}),
}


def _services() -> dict:
    raw = yaml.safe_load((REPO / COMPOSE_PATH).read_text(encoding="utf-8"))
    return (raw or {}).get("services") or {}


def _default_profile_services() -> dict:
    """Services started by a bare ``docker compose up`` (no ``profiles:`` gate)."""
    return {
        name: spec
        for name, spec in _services().items()
        if isinstance(spec, dict) and not spec.get("profiles")
    }


def _host_interface(mapping) -> str:
    """The interface a ``ports:`` entry publishes on.

    Every entry publishes something; only an explicit prefix narrows it. Docker
    reads a two-part ``"5432:5432"`` as 0.0.0.0 and a three-part
    ``"127.0.0.1:5432:5432"`` as loopback. The long dict form carries the
    interface in ``host_ip``, which likewise defaults to 0.0.0.0 when absent.
    Splitting from the right keeps IPv6 literals (``::1``, ``[::1]``) intact.
    """
    if isinstance(mapping, dict):
        return str(mapping.get("host_ip") or "0.0.0.0").strip("[]")
    parts = str(mapping).split("/")[0].rsplit(":", 2)
    return parts[0].strip("[]") if len(parts) == 3 else "0.0.0.0"


def _host_port(mapping) -> str | None:
    """The host-side port of a ``ports:`` entry, or ``None`` if ephemeral."""
    if isinstance(mapping, dict):
        published = mapping.get("published")
        return None if published is None else str(published)
    parts = str(mapping).split("/")[0].rsplit(":", 2)
    if len(parts) == 3:  # host_ip:host:container
        return parts[1]
    if len(parts) == 2:  # host:container
        return parts[0]
    return None  # bare container port -> Docker picks a random host port


def _published(spec: dict) -> list[tuple[str, str, str | None]]:
    return [
        (str(m), _host_interface(m), _host_port(m)) for m in (spec.get("ports") or [])
    ]


def test_compose_file_is_where_this_test_thinks_it_is() -> None:
    # The file already moved once (docker/ -> infra/docker/) after #587 was
    # filed. A missing file surfaces as a collection error from the parametrize
    # read above, which is loud enough on its own; what this catches is the
    # quieter case of a file that still parses but yields no services.
    assert (
        _default_profile_services()
    ), f"no profile-less services parsed from {COMPOSE_PATH}"


@pytest.mark.parametrize("service", sorted(_default_profile_services()))
def test_default_service_ports_bind_loopback(service: str) -> None:
    spec = _default_profile_services()[service]

    # network_mode: host publishes everything on every interface and declares no
    # ports:, so it would otherwise sail through with nothing to check.
    assert spec.get("network_mode") != "host", (
        f"{service} uses network_mode: host, which publishes every listening "
        f"port on every interface and bypasses this gate entirely."
    )

    allowed = SHARED_HOST_PORTS.get(service, frozenset())
    wide = [
        raw
        for raw, interface, port in _published(spec)
        if interface not in LOOPBACK and port not in allowed
    ]
    assert not wide, (
        f"{service} publishes {wide} on a non-loopback interface in {COMPOSE_PATH}. "
        f"Prefix the mapping with 127.0.0.1: , or if that exact port genuinely "
        f"has an off-host consumer, add it to SHARED_HOST_PORTS[{service!r}] and "
        f"say why (#587)."
    )


def test_shared_host_ports_name_real_services() -> None:
    # Keeps the allowlist from rotting into names that no longer exist, which
    # would silently stop exempting anything -- or worse, stop gating it.
    unknown = set(SHARED_HOST_PORTS) - set(_services())
    assert (
        not unknown
    ), f"SHARED_HOST_PORTS names services not in compose: {sorted(unknown)}"


@pytest.mark.parametrize("service", sorted(SHARED_HOST_PORTS))
def test_allowlisted_service_ports_bind_loopback(service: str) -> None:
    # soc-daemon sits behind profiles: [daemon], so the default-profile gate
    # above never sees it (#1683).
    wide = [
        raw
        for raw, interface, port in _published(_services()[service])
        if interface not in LOOPBACK and port not in SHARED_HOST_PORTS[service]
    ]
    assert not wide, (
        f"{service} publishes {wide} on a non-loopback interface in {COMPOSE_PATH}. "
        f"Prefix the mapping with 127.0.0.1: or allowlist the port in "
        f"SHARED_HOST_PORTS[{service!r}] with a reason."
    )
