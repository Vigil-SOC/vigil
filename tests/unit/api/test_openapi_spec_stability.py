"""The OpenAPI spec must be a pure function of the source tree.

``clients/web/src/services/generated/schema.d.ts`` is committed and CI fails
on a regen diff (``scripts/generate_frontend_types.py``). That only works if
two people running the generator get the same spec, which means no route may
enter the schema because of local state — a frontend build sitting in
``clients/web/build``, a feature flag exported in the shell, a context path.

The generator pins the env vars that gate routers; this guards the other half,
where a route outside the API surface slips into the schema. The SPA fallback
in ``services/api/main.py`` is the case that bit us: it is registered only when
a build directory exists, so anyone who had run ``npm run build`` regenerated a
schema with an extra ``/{full_path}`` entry and could not make CI green.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = pytest.mark.unit

# The whole documented surface. Anything else reaching the schema is either a
# route that should carry include_in_schema=False, or a sign that the spec is
# being generated under a context path.
DOCUMENTED_PREFIXES = ("/api/", "/internal/")


def test_schema_holds_only_api_routes():
    from services.api.main import app

    paths = app.openapi()["paths"]
    assert paths, "app.openapi() reported no paths at all"

    stray = sorted(p for p in paths if not p.startswith(DOCUMENTED_PREFIXES))
    assert not stray, (
        f"{len(stray)} path(s) in the OpenAPI schema are outside "
        f"{DOCUMENTED_PREFIXES}. A non-API route (SPA fallback, static handler) "
        f"belongs behind include_in_schema=False — leaving it in makes the "
        f"committed frontend types depend on the machine that generated them:\n  - "
        + "\n  - ".join(stray)
    )
