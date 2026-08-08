"""Discover and mount API routers by scanning the filesystem (issues #478, #488).

A router lives with its domain as ``core/<domain>/<name>_router.py`` once that
domain is in ``core/``; until then a router that still reaches into un-migrated
code is parked under ``services/api/routers/<name>.py``. This module globs both
locations, imports each module, and mounts its ``router`` using the declarative
``ROUTER_META`` (see ``core/routing.py``). Adding a router needs no edit here or
in ``services/api/main.py``.

A router's **short name** is its filename minus any ``_router`` suffix
(``core/cases/case_metrics_router.py`` and a parked ``cases.py`` read as
``case_metrics``/``cases``). Mount order is alphabetical by that name, matching
the pre-#488 order. That stays safe because no route in one router shadows a
route in another —
``tests/unit/test_router_discovery.py::test_no_cross_router_path_shadowing``
fails loudly the day that stops being true.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Sequence

from fastapi import FastAPI

from core.routing import Auth, RouterMeta

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_DIR = _REPO_ROOT / "core"
_PARKED_DIR = _REPO_ROOT / "services" / "api" / "routers"


def _short_name(path: Path) -> str:
    stem = path.stem
    return stem[: -len("_router")] if stem.endswith("_router") else stem


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts)


def _router_files() -> list[Path]:
    """Every candidate router file: colocated in core/, or parked."""
    files: list[Path] = []
    # Colocated: core/<domain>/[.../]<name>_router.py — recurse so vendor
    # slices like core/integrations/cloudflare/ are included.
    for p in _CORE_DIR.rglob("*_router.py"):
        if "__pycache__" not in p.parts:
            files.append(p)
    # Parked: every module directly under services/api/routers/.
    if _PARKED_DIR.is_dir():
        for p in _PARKED_DIR.glob("*.py"):
            if not p.name.startswith("__"):
                files.append(p)
    return files


def _router_map() -> dict[str, Path]:
    """Short name -> file path, raising on an ambiguous collision."""
    mapping: dict[str, Path] = {}
    for p in _router_files():
        name = _short_name(p)
        if name in mapping:
            raise RuntimeError(
                f"two router modules resolve to the short name {name!r}: "
                f"{mapping[name].relative_to(_REPO_ROOT)} and "
                f"{p.relative_to(_REPO_ROOT)}. Rename one so discovery, mount "
                f"order, and logs stay unambiguous."
            )
        mapping[name] = p
    return mapping


def iter_router_modules() -> list[str]:
    """Return the sorted short names of all discoverable routers."""
    return sorted(_router_map())


def load_router_specs() -> list[tuple[str, object, RouterMeta]]:
    """Import every router module and return ``(short_name, router, meta)``.

    Raises if a module is missing ``router`` or ``ROUTER_META`` — a missing
    declaration must be a startup failure, never a silently unmounted or
    mis-mounted route. Sorted by short name so mount order is stable.
    """
    specs: list[tuple[str, object, RouterMeta]] = []
    for name, path in sorted(_router_map().items()):
        dotted = _dotted(path)
        module = importlib.import_module(dotted)

        router = getattr(module, "router", None)
        if router is None:
            raise RuntimeError(
                f"{dotted} defines no `router`. Every discovered module must "
                f"export one; a non-router helper must not match the scan "
                f"(*_router.py under core/, or any module under "
                f"services/api/routers/)."
            )

        meta = getattr(module, "ROUTER_META", None)
        if meta is None:
            raise RuntimeError(
                f"{dotted} defines no `ROUTER_META` (see core/routing.py) — "
                f"prefixes are not inferred from filenames, because that would "
                f"be wrong for half of them."
            )
        if not isinstance(meta, RouterMeta):
            raise RuntimeError(
                f"{dotted} ROUTER_META must be a RouterMeta, got "
                f"{type(meta).__name__}"
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

    ``auth_dependency`` is injected rather than imported so this module stays a
    leaf (it must not import ``services.api.main``, which imports it). Returns
    the short names actually mounted, for logging and for the discovery test to
    assert against.
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
