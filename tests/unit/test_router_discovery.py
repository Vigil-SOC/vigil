"""Guards for the router auto-discovery introduced in issue #478.

The refactor replaced 42 hand-written ``include_router`` calls with a
``pkgutil`` scan over ``backend/api/``. These tests lock in the invariants
that made that safe, so the assumptions can't rot silently:

* every router module declares ``ROUTER_META`` (no convention fallback,
  because filename-inferred prefixes are wrong for 21 of 42 modules);
* mount order stays irrelevant — no route in one router can shadow a route
  in another;
* feature-gated inbound webhook receivers stay unmounted by default.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = pytest.mark.unit


def _specs():
    from api._discovery import load_router_specs

    return load_router_specs()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """The webhook gates read the ``lru_cache``'d ``Settings``, so a test that
    mutates the gate env vars must rebuild it — and must not leak that build
    into the next test."""
    from core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_every_api_module_declares_router_meta():
    """``load_router_specs`` raises on a module missing ``router``/``ROUTER_META``.

    Reaching this assertion at all means all of them declared both.
    """
    from api._discovery import iter_router_modules

    specs = _specs()
    assert len(specs) == len(iter_router_modules())
    assert len(specs) >= 40, f"only discovered {len(specs)} routers — scan broken?"


def test_prefixes_are_declared_not_inferred():
    """Guard the reason ROUTER_META is mandatory.

    If prefixes could be inferred from module names this whole mechanism
    would be unnecessary — so assert that they genuinely can't be. A future
    reader tempted to "simplify" by dropping ROUTER_META gets this failure
    as the explanation.
    """
    mismatches = [
        (name, meta.prefix)
        for name, _router, meta in _specs()
        if meta.prefix != "/api/" + name.replace("_", "-")
    ]
    assert len(mismatches) > 10, (
        "Filename-inferred prefixes now match almost everywhere, which would "
        "undermine the rationale for mandatory ROUTER_META. Re-check the "
        "design before relaxing anything."
    )


def _to_pattern(path: str) -> re.Pattern:
    """Turn ``/api/x/{id}`` into a regex matching a concrete path.

    A ``{name:path}`` convertor matches across ``/`` (multi-segment); every
    other ``{...}`` matches a single segment. Modelling ``:path`` as one
    segment would under-report the literals such a route can swallow.
    """

    def _seg(m: "re.Match[str]") -> str:
        return ".+" if m.group(0).endswith(":path}") else "[^/]+"

    escaped = re.escape(path).replace(r"\{", "{").replace(r"\}", "}")
    return re.compile("^" + re.sub(r"\{[^}]+\}", _seg, escaped) + "$")


def test_no_cross_router_path_shadowing():
    """Mount order must stay irrelevant.

    FastAPI resolves overlapping routes first-match-wins, and discovery mounts
    alphabetically. That is only safe while no route in one router can capture a
    request meant for a *different* router. Shadowing inside one router is fine
    — ``/api/findings/{finding_id}`` vs ``/api/findings/all`` — because
    intra-router order comes from decorator order and discovery never changes
    it.

    Two routes only collide when they share an HTTP method: Starlette falls
    through a path match whose method doesn't match, so method sets must
    intersect for mount order to matter. If this fails, the named routers now
    depend on mount order and ROUTER_META needs an explicit ordering field.
    """
    routes = []  # (full_path, methods, owner)
    for name, router, meta in _specs():
        for route in router.routes:
            full = meta.prefix + getattr(route, "path", "")
            methods = frozenset(getattr(route, "methods", None) or ())
            routes.append((full, methods, name))

    # Identical path in two routers, sharing a method: never looks like
    # "param vs literal", so the comparison below would miss it entirely.
    exact = [
        (a_p, a_o, b_o)
        for i, (a_p, a_m, a_o) in enumerate(routes)
        for b_p, b_m, b_o in routes[i + 1:]
        if a_p == b_p and a_o != b_o and (a_m & b_m)
    ]

    # A parameterised path in one router swallowing a literal in another.
    params = [r for r in routes if "{" in r[0]]
    literals = [r for r in routes if "{" not in r[0]]
    swallow = [
        (p_p, p_o, l_p, l_o)
        for p_p, p_m, p_o in params
        for l_p, l_m, l_o in literals
        if p_o != l_o and (p_m & l_m) and _to_pattern(p_p).match(l_p)
    ]

    problems = [f"  identical path {p} in {a} and {b}" for p, a, b in exact] + [
        f"  {pp} ({po}) shadows {lp} ({lo})" for pp, po, lp, lo in swallow
    ]
    assert not problems, (
        "Cross-router path shadowing — mount order now matters:\n"
        + "\n".join(problems)
    )


GATE_ENV_VARS = ("DARKTRACE_ENABLED", "CLOUDY_INGESTION_ENABLED")


def test_every_public_webhook_declares_a_gate():
    from api._meta import Auth

    gated = [
        (name, meta) for name, _r, meta in _specs() if meta.auth is Auth.PUBLIC_WEBHOOK
    ]
    assert gated, "expected at least one PUBLIC_WEBHOOK router"
    for name, meta in gated:
        assert meta.enabled is not None, f"{name} is PUBLIC_WEBHOOK without a gate"


def test_gated_webhook_receivers_are_off_by_default(monkeypatch):
    """An inbound receiver must never be exposed just by existing.

    The env is cleared explicitly rather than asserted clean: a developer's
    ``.env`` (or another test) may well set these flags, and this test must
    describe the shipped default rather than whatever the ambient environment
    happens to be.
    """
    from api._meta import Auth
    from core.config import get_settings

    for var in GATE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()

    for name, _r, meta in _specs():
        if meta.auth is Auth.PUBLIC_WEBHOOK:
            assert not meta.is_enabled, f"{name} would be mounted by default"


@pytest.mark.parametrize(
    "module,var",
    [
        ("darktrace_webhook", "DARKTRACE_ENABLED"),
        ("cloudflare_webhooks", "CLOUDY_INGESTION_ENABLED"),
    ],
)
def test_gate_actually_opens_when_flag_set(monkeypatch, module, var):
    """The other half: a gate that can never open would be just as wrong.

    Without this, ``enabled=lambda: False`` would satisfy the default-off test
    while silently disabling the integration for everyone.
    """
    from core.config import get_settings

    for v in GATE_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    get_settings.cache_clear()
    meta = dict((n, m) for n, _r, m in _specs())[module]
    assert not meta.is_enabled

    monkeypatch.setenv(var, "true")
    get_settings.cache_clear()
    assert meta.is_enabled, f"{module} stays off even with {var}=true"


def test_public_webhook_requires_a_gate():
    """RouterMeta refuses to construct a PUBLIC_WEBHOOK without ``enabled``."""
    from api._meta import Auth, RouterMeta

    with pytest.raises(ValueError, match="must declare `enabled`"):
        RouterMeta(
            prefix="/api/webhooks/x",
            tags=["x"],
            auth=Auth.PUBLIC_WEBHOOK,
            reason="inbound machine caller, HMAC verified at the endpoint",
        )


def test_weakening_auth_requires_a_written_reason():
    """A non-REQUIRED posture cannot be adopted silently."""
    from api._meta import Auth, RouterMeta

    with pytest.raises(ValueError, match="no `reason`"):
        RouterMeta(prefix="/api/x", tags=["x"], auth=Auth.ROUTER_MANAGED)

    # whitespace is not a justification
    with pytest.raises(ValueError, match="no `reason`"):
        RouterMeta(prefix="/api/x", tags=["x"], auth=Auth.ROUTER_MANAGED, reason="   ")


def test_required_auth_rejects_a_reason():
    """Keeps ``reason`` meaning "why auth is weaker", not a notes field."""
    from api._meta import Auth, RouterMeta

    with pytest.raises(ValueError, match="needs no `reason`"):
        RouterMeta(prefix="/api/x", tags=["x"], auth=Auth.REQUIRED, reason="some note")


def test_every_non_required_router_has_a_reason():
    """The live tree, not just the validator: all 5 deviations are justified."""
    from api._meta import Auth

    deviations = [
        (name, meta) for name, _r, meta in _specs() if meta.auth is not Auth.REQUIRED
    ]
    assert len(deviations) == 5, (
        f"expected 5 non-REQUIRED routers, found {len(deviations)}: "
        f"{sorted(n for n, _ in deviations)}. A new one needs review."
    )
    for name, meta in deviations:
        assert meta.reason.strip(), f"{name} deviates from REQUIRED without a reason"


@pytest.mark.parametrize("bad", ["api/x", "/api/x/"])
def test_prefix_shape_is_validated(bad):
    from api._meta import Auth, RouterMeta

    with pytest.raises(ValueError):
        RouterMeta(prefix=bad, tags=["x"], auth=Auth.REQUIRED)
