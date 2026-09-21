#!/usr/bin/env python3
"""Dump the FastAPI OpenAPI spec and generate frontend TypeScript types.

Does not start a server: uses ``app.openapi()``, the same path as
``tests/security/test_route_auth_coverage.py``. Output is committed under
``clients/web/src/services/generated/`` — do not edit it by hand.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "clients" / "web"
OUT_DIR = WEB / "src" / "services" / "generated"
OUT_FILE = OUT_DIR / "schema.d.ts"
OPENAPI_TS = WEB / "node_modules" / ".bin" / "openapi-typescript"

# The spec must be a pure function of the source tree: regenerating on a
# developer's machine and in CI has to produce byte-identical output, or the
# CI diff check fails for reasons that have nothing to do with the API.
#
# Every one of these is assignment, not setdefault — an exported value in the
# caller's shell is exactly what we are defending against.
#
#   DEV_MODE / VIGIL_DISABLE_DOTENV — importing the app must not demand
#     JWT_SECRET_KEY or read a developer's .env. Neither changes the spec.
#   VIGIL_CONTEXT_PATH — prefixes *every* route (services/api/main.py), so a
#     non-empty value rewrites all of them and the diff is 100%.
#   DARKTRACE_ENABLED / CLOUDY_INGESTION_ENABLED — RouterMeta.enabled gates,
#     evaluated at mount time. When on, their webhook routes join the spec.
#   VIGIL_DIR — the State Directory holding the encrypted secrets store, and so
#     the POSTGRESQL_CONNECTION_STRING that DatabaseConfig prefers. Pointed at an
#     empty temp dir together with an unusable POSTGRES_* target because
#     cloudy_ingestion_enabled() consults system_config in the database before
#     falling back to its setting: without this the spec depends on the contents
#     of whatever instance the developer happens to have running.
_STATE_DIR = tempfile.mkdtemp(prefix="vigil-openapi-")
os.environ.update(
    {
        "DEV_MODE": "true",
        "VIGIL_DISABLE_DOTENV": "1",
        "VIGIL_CONTEXT_PATH": "",
        "DARKTRACE_ENABLED": "false",
        "CLOUDY_INGESTION_ENABLED": "false",
        "VIGIL_DIR": _STATE_DIR,
        # Refused, not filtered: a closed port fails immediately, where an
        # unroutable host would stall the dump on a connect timeout.
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "1",
    }
)


def _stable_operation_ids(spec: dict) -> None:
    """Rewrite operationIds from path+method.

    FastAPI derives the id from the Python function. A catch-all that handles
    several verbs therefore collides, and *which* verb FastAPI stamps on the
    shared id is not stable across ``app.openapi()`` calls — CI regen would
    flake. Path+method is unique and deterministic.
    """
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        slug = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        for method, op in methods.items():
            if not isinstance(op, dict) or "operationId" not in op:
                continue
            op["operationId"] = f"{method}_{slug}"


def _dump_spec(path: Path) -> None:
    from services.api.main import app

    spec = app.openapi()
    _stable_operation_ids(spec)
    path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    if not OPENAPI_TS.is_file():
        print(
            f"missing {OPENAPI_TS}; run `npm ci` in clients/web first",
            file=sys.stderr,
        )
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        spec_path = Path(tmp.name)

    try:
        _dump_spec(spec_path)
        subprocess.run(
            [str(OPENAPI_TS), str(spec_path), "-o", str(OUT_FILE)],
            check=True,
        )
    finally:
        spec_path.unlink(missing_ok=True)
        shutil.rmtree(_STATE_DIR, ignore_errors=True)

    print(f"wrote {OUT_FILE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
