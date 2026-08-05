"""Declarative mount metadata for API routers.

Every module in ``backend/api/`` exports a ``router`` and a ``ROUTER_META``
describing how that router should be mounted. ``backend/api/_discovery.py``
scans the package and mounts them, so adding a router needs no edit to
``backend/main.py`` (issue #478).

This module is deliberately a leaf: it imports nothing from ``backend.main``
or from any router module, so the 42 api modules can import ``Auth`` and
``RouterMeta`` without an import cycle.

``ROUTER_META`` is **mandatory**. There is no convention-based fallback,
because filename-inferred prefixes would be wrong for 21 of the 42 modules
(``ai_config`` -> ``/api/ai``, ``case_metrics`` -> ``/api/cases/metrics``,
``llm_providers`` -> ``/api/llm/providers``, ``ingestion`` -> ``/api/ingest``,
and 17 more). A module without ``ROUTER_META`` is a startup error, not a
silently mis-mounted route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Sequence


class Auth(Enum):
    """Why a router has the auth posture it has.

    Recording the reason rather than a bare boolean means the five
    non-``REQUIRED`` routers each carry their justification in code. A wrong
    value is a visible claim a reviewer can challenge, rather than an
    omission nobody notices.
    """

    #: Mount with the shared authenticated-active-user dependency. The
    #: default posture; 37 of 42 routers.
    REQUIRED = "required"

    #: The router enforces its own auth internally — a nested router with
    #: its own ``dependencies``, a per-handler ``Depends(...)``, or a bearer
    #: API-key check. Do not attach the shared dependency: doing so would
    #: break login (``auth``) or the inbound bearer path (``vstrike``).
    ROUTER_MANAGED = "router_managed"

    #: Inbound third-party webhook receiver. Unauthenticated by session
    #: because the caller is a machine; the endpoint itself must do
    #: HMAC/API-key validation. Always pair with ``enabled`` so the receiver
    #: is off unless explicitly switched on.
    PUBLIC_WEBHOOK = "public_webhook"


@dataclass(frozen=True)
class RouterMeta:
    """How to mount one router.

    :param prefix: Path prefix **excluding** any deployment context path;
        the mounting code prepends ``VIGIL_CONTEXT_PATH``. Declared rather
        than inferred — see the module docstring.
    :param tags: OpenAPI tags.
    :param auth: Auth posture. See :class:`Auth`.
    :param reason: Why this router deviates from ``Auth.REQUIRED``. Mandatory
        for every non-``REQUIRED`` posture and rejected for ``REQUIRED``,
        where there is nothing to justify. A field rather than a comment so
        the justification cannot be deleted or omitted silently. To enumerate
        every deviation and its rationale, iterate
        ``api._discovery.load_router_specs()`` — a bare ``grep reason=``
        false-matches unrelated kwargs such as ``change_reason=``. The
        enumeration is asserted by
        ``tests/unit/test_router_discovery.py::test_every_non_required_router_has_a_reason``.
    :param enabled: Optional predicate evaluated at mount time. When it
        returns false the router is not mounted at all. Required for
        ``PUBLIC_WEBHOOK`` routers so an inbound receiver cannot be exposed
        by default.
    :param extra_dependencies: Additional ``Depends(...)`` beyond the auth
        dependency — e.g. the per-router rate limit on ``claude``.
    """

    prefix: str
    tags: Sequence[str]
    auth: Auth
    reason: str = ""
    enabled: Callable[[], bool] | None = None
    extra_dependencies: Sequence = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.prefix.startswith("/"):
            raise ValueError(f"prefix must start with '/': {self.prefix!r}")
        if self.prefix.endswith("/"):
            raise ValueError(f"prefix must not end with '/': {self.prefix!r}")
        if self.auth is Auth.PUBLIC_WEBHOOK and self.enabled is None:
            # An inbound receiver that is always mounted is exactly the
            # accident this refactor must not introduce.
            raise ValueError(
                f"PUBLIC_WEBHOOK router at {self.prefix!r} must declare "
                "`enabled` so it cannot be exposed by default"
            )
        if self.auth is not Auth.REQUIRED and not self.reason.strip():
            # Weakening auth is the one change here that should never be
            # possible to make quietly.
            raise ValueError(
                f"router at {self.prefix!r} declares auth={self.auth.name} "
                "but no `reason`. Every deviation from Auth.REQUIRED must say "
                "why in the metadata, not in a comment."
            )
        if self.auth is Auth.REQUIRED and self.reason.strip():
            # Otherwise `reason` drifts into a general-purpose notes field and
            # stops meaning "here is why auth is weaker than the default".
            raise ValueError(
                f"router at {self.prefix!r} is Auth.REQUIRED and needs no "
                "`reason`; use a normal comment for other notes."
            )

    @property
    def is_enabled(self) -> bool:
        return True if self.enabled is None else bool(self.enabled())
