"""Scan ``backend/api/`` and mount every router it finds (issue #478).

Replaces 42 hand-written ``include_router`` calls plus a partial
re-export list in ``backend/api/__init__.py``. Adding a router now means
adding a module with a ``router`` and a ``ROUTER_META`` — no edit to
``backend/main.py``.

Mount order is alphabetical (whatever ``pkgutil.iter_modules`` yields).
That is safe: FastAPI resolves overlapping paths first-match-wins, and
across the current 42 routers there are **zero** cross-router path
shadows — all 30 param-vs-literal shadows (e.g.
``/api/findings/{finding_id}`` vs ``/api/findings/all``) sit inside a
single router, where ordering comes from decorator order in the module
and is unaffected by how routers are mounted.
``tests/unit/test_router_discovery.py`` asserts that stays true, so the
day a cross-router shadow appears it fails loudly instead of silently
resolving to the wrong handler.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Sequence

from fastapi import FastAPI

from api._meta import Auth, RouterMeta

logger = logging.getLogger(__name__)

#: Modules in backend/api/ that are not routers.
_SKIP = {"_meta", "_discovery"}

_PACKAGE_DIR = Path(__file__).resolve().parent


def iter_router_modules() -> list[str]:
    """Return the sorted names of candidate router modules."""
    names = [
        name
        for _finder, name, is_pkg in pkgutil.iter_modules([str(_PACKAGE_DIR)])
        if not is_pkg and not name.startswith("__") and name not in _SKIP
    ]
    return sorted(names)


def load_router_specs() -> list[tuple[str, object, RouterMeta]]:
    """Import every router module and return ``(name, router, meta)``.

    Raises if a module is missing ``router`` or ``ROUTER_META``. A missing
    declaration must be a startup failure, never a silently unmounted or
    mis-mounted route.
    """
    specs: list[tuple[str, object, RouterMeta]] = []
    for name in iter_router_modules():
        module = importlib.import_module(f"api.{name}")

        router = getattr(module, "router", None)
        if router is None:
            raise RuntimeError(
                f"backend/api/{name}.py defines no `router`. Every module in "
                f"this package must export one, or be listed in "
                f"_discovery._SKIP if it is not a router."
            )

        meta = getattr(module, "ROUTER_META", None)
        if meta is None:
            raise RuntimeError(
                f"backend/api/{name}.py defines no `ROUTER_META`. Declare one "
                f"(see backend/api/_meta.py) — prefixes are not inferred from "
                f"filenames, because that would be wrong for half of them."
            )
        if not isinstance(meta, RouterMeta):
            raise RuntimeError(
                f"backend/api/{name}.py ROUTER_META must be a RouterMeta, "
                f"got {type(meta).__name__}"
            )

        specs.append((name, router, meta))
    return specs


def mount_routers(
    app: FastAPI,
    *,
    context_path: str = "",
    auth_dependency: Sequence | None = None,
) -> list[str]:
    """Mount every discovered router onto ``app``.

    ``auth_dependency`` is injected rather than imported so this module
    stays independent of ``backend.main`` (which imports it).

    Returns the names of the modules actually mounted, for logging and for
    the discovery test to assert against.
    """
    auth_dependency = list(auth_dependency or [])
    mounted: list[str] = []
    skipped: list[str] = []

    for name, router, meta in load_router_specs():
        if not meta.is_enabled:
            # Feature-gated, currently off. Not mounting at all is the point:
            # the endpoint's own flag check is a second line of defence, not
            # the first.
            skipped.append(name)
            continue

        dependencies = list(meta.extra_dependencies)
        if meta.auth is Auth.REQUIRED:
            dependencies = [*auth_dependency, *dependencies]

        app.include_router(
            router,
            prefix=f"{context_path}{meta.prefix}",
            tags=list(meta.tags),
            dependencies=dependencies,
        )
        mounted.append(name)

    if skipped:
        logger.info(
            "Routers not mounted (feature-gated off): %s", ", ".join(sorted(skipped))
        )
    logger.info("Mounted %d API routers", len(mounted))
    return mounted
