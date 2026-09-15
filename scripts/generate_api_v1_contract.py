#!/usr/bin/env python3
"""Generate the committed contract snapshot for the frozen ``/api/v1`` surface.

The snapshot is the promise: the paths and response/request shapes of
``/api/v1/**``, minus any operation flagged ``x-vigil-beta`` (versioned but not
frozen). It is a subset of the app's OpenAPI document — never the whole spec,
which still carries console wiring (``/api/config``, ``/api/orchestrator``, …)
that must stay free to change.

``tests/unit/api/test_api_v1_contract.py`` rebuilds this and fails if it drifts
from the committed file, so a change to a frozen shape is a red build, not a
silent edit. To intentionally change the contract, run this script and commit
the diff — a deliberate, reviewable act.

    python scripts/generate_api_v1_contract.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "core" / "api" / "v1" / "contract.snapshot.json"

V1_PREFIX = "/api/v1"
BETA_FLAG = "x-vigil-beta"


def _stable_operation_ids(spec: dict) -> None:
    """Rewrite operationIds from path+method (same rule as the types export).

    FastAPI derives the id from the Python function; a catch-all handling
    several verbs collides and is not stable across ``app.openapi()`` calls.
    Path+method is unique and deterministic.
    """
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        for method, op in methods.items():
            if isinstance(op, dict) and "operationId" in op:
                op["operationId"] = f"{method}_{slug}"


def _referenced_schemas(node: Any, out: set[str]) -> None:
    """Collect component schema names referenced anywhere under ``node``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            m = re.fullmatch(r"#/components/schemas/(.+)", ref)
            if m:
                out.add(m.group(1))
        for v in node.values():
            _referenced_schemas(v, out)
    elif isinstance(node, list):
        for v in node:
            _referenced_schemas(v, out)


def build_contract(app: Any) -> dict:
    """Return the frozen-subset contract dict from a FastAPI ``app``.

    Deterministic: stable operation ids, only ``/api/v1`` paths, beta operations
    dropped, and the transitive closure of referenced component schemas so a
    change to a shared response shape is caught, not just a path rename.
    """
    spec = app.openapi()
    _stable_operation_ids(spec)

    HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}

    paths: dict[str, Any] = {}
    for path, methods in (spec.get("paths") or {}).items():
        if not path.startswith(V1_PREFIX) or not isinstance(methods, dict):
            continue
        kept = {}
        for method, op in methods.items():
            if method.lower() in HTTP_METHODS and isinstance(op, dict):
                if op.get(BETA_FLAG) is True:
                    continue  # versioned but not frozen
                kept[method] = op
            elif method.lower() not in HTTP_METHODS:
                kept[method] = op  # parameters/summary at the path level
        if any(m.lower() in HTTP_METHODS for m in kept):
            paths[path] = kept

    # Transitive closure of referenced schemas.
    all_schemas = (spec.get("components") or {}).get("schemas") or {}
    wanted: set[str] = set()
    _referenced_schemas(paths, wanted)
    frontier = set(wanted)
    while frontier:
        name = frontier.pop()
        schema = all_schemas.get(name)
        if schema is None:
            continue
        found: set[str] = set()
        _referenced_schemas(schema, found)
        for n in found - wanted:
            wanted.add(n)
            frontier.add(n)

    components = {"schemas": {k: all_schemas[k] for k in sorted(wanted) if k in all_schemas}}

    return {
        "openapi": spec.get("openapi", ""),
        "x-contract": "vigil /api/v1 frozen surface; excludes x-vigil-beta operations",
        "paths": paths,
        "components": components,
    }


def serialize(contract: dict) -> str:
    return json.dumps(contract, indent=2, sort_keys=True) + "\n"


def _load_app():
    os.environ.setdefault("JWT_SECRET_KEY", "contract-snapshot-only")
    os.environ.setdefault("DEV_MODE", "true")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from services.api.main import app

    return app


def main() -> int:
    SNAPSHOT.write_text(serialize(build_contract(_load_app())))
    print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
