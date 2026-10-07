"""The frozen contract answers at the address it promises, in a real install.

`core/api/v1/contract.snapshot.json` is what external callers are handed, and
`core/api/v1/README.md` tells them to use it. Neither is worth anything if the
path only answers on a machine that has not built the frontend.

The SPA fallback in `services/api/main.py` is registered only when
`clients/web/build/index.html` exists, and the backend CI job never builds the
frontend -- so every test that talks to the app talks to a routing table that
no install has. These tests put the fallback back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SNAPSHOT = Path(__file__).resolve().parents[3] / "core/api/v1/contract.snapshot.json"


def _frozen_paths() -> dict:
    return json.loads(SNAPSHOT.read_text())["paths"]


# The frozen paths that take no parameter: the collection roots an external
# caller types first, and the ones a trailing slash can silently move.
def _collection_roots() -> list[tuple[str, str]]:
    return sorted(
        (method.upper(), path)
        for path, ops in _frozen_paths().items()
        if "{" not in path
        for method in ops
        if method.lower() in ("get", "post")
    )


@pytest.fixture
def client_with_a_frontend_build():
    """The app as an install serves it: the SPA fallback in place.

    Registered the same way main.py registers it -- as a handler for a 404 that
    has already been decided, not as a route that decides one -- so what is
    under test here is the real arrangement rather than a copy of it.
    """
    from fastapi.exception_handlers import http_exception_handler
    from fastapi.responses import HTMLResponse
    from fastapi.testclient import TestClient
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from services.api.main import app, serves_the_app_shell

    async def app_shell_or_error(request, exc):
        if exc.status_code == 404 and serves_the_app_shell(
            request.url.path, request.method
        ):
            return HTMLResponse("<html></html>")
        return await http_exception_handler(request, exc)

    previous = app.exception_handlers.get(StarletteHTTPException)
    app.add_exception_handler(StarletteHTTPException, app_shell_or_error)
    # Starlette builds the middleware stack once and the exception middleware
    # keeps its own copy of the handlers, so a handler added after some earlier
    # test has already started the app is simply not in force. Dropping the
    # stack makes the next start rebuild it; dropping it again afterwards
    # leaves the app as it was found.
    app.middleware_stack = None
    try:
        with TestClient(app) as client:
            yield client
    finally:
        if previous is None:
            app.exception_handlers.pop(StarletteHTTPException, None)
        else:
            app.exception_handlers[StarletteHTTPException] = previous
        app.middleware_stack = None


def test_every_frozen_collection_answers_at_the_path_it_promises(
    client_with_a_frontend_build,
):
    """401 is the surface answering. 200, 404 or 405 is something else."""
    wrong = {}
    for method, path in _collection_roots():
        response = client_with_a_frontend_build.request(
            method, path, follow_redirects=False
        )
        if response.status_code != 401:
            wrong[f"{method} {path}"] = response.status_code

    assert not wrong, (
        f"frozen paths that did not reach their route: {wrong}. A collection "
        'registered as `@router.get("/")` is frozen with a trailing slash, so '
        "the path the snapshot and the README give falls through to the SPA "
        "fallback -- which is GET-only, so a POST answers 405 and a GET answers "
        "the fallback's own body."
    )


def test_the_frozen_collections_are_spelled_one_way():
    """Two of five were bare and three carried a slash, from one character."""
    trailing = [p for p in _frozen_paths() if p.endswith("/")]
    assert not trailing, (
        f"frozen collection paths carrying a trailing slash: {trailing}. The "
        "contract promises one spelling; core/api/v1/README.md gives the bare "
        "one, and a caller who types it must not get a redirect or a fallback."
    )


def test_an_api_path_that_matches_no_route_is_a_404(client_with_a_frontend_build):
    """It used to be a 200 carrying a two-element array, on every install."""
    response = client_with_a_frontend_build.get("/api/v1/nothing-here")

    assert response.status_code == 404, (
        "a miss under /api answered "
        f"{response.status_code} with {response.text[:60]!r}. `return {{...}}, "
        "404` is Flask; FastAPI serialises the tuple and keeps the 200, so a "
        "caller checking response.ok parsed an error body as a result."
    )
    assert response.json() == {"detail": "Not Found"}


def test_the_app_shell_still_answers_a_path_that_is_not_the_api(
    client_with_a_frontend_build,
):
    """The fallback's actual job, which the 404 above must not have taken."""
    response = client_with_a_frontend_build.get("/cases/some-case-id")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "path",
    ["/api/findings/", "/api/cases/", "/api/v1/findings/", "/api/v1/cases/"],
)
def test_the_other_spelling_is_redirected_rather_than_swallowed(
    client_with_a_frontend_build, path
):
    """A caller who types the slash is sent to the route, not told it is gone.

    The legacy mounts exist so callers that predate the versioned surface keep
    working, and the slash is the spelling they were using. A fallback that
    claims every address answers these itself; one that runs after routing has
    failed leaves Starlette free to redirect them.
    """
    response = client_with_a_frontend_build.get(path, follow_redirects=False)

    assert response.status_code == 307, (
        f"GET {path} answered {response.status_code}. The bare form is the "
        "route; the slash form has to reach it rather than 404."
    )
    assert response.headers["location"].endswith(path.rstrip("/"))


@pytest.mark.parametrize(
    "path", ["/static/nope.js", "/assets/nope.css", "/internal/nope", "/mcp"]
)
def test_a_miss_under_a_backend_prefix_is_not_the_app_shell(
    client_with_a_frontend_build, path
):
    """The SPA's router knows nothing about these, so HTML is the wrong answer.

    For a bundle it is worse than wrong: the browser refuses to execute HTML as
    a module, which is the failure the /assets mount above exists to prevent.
    """
    response = client_with_a_frontend_build.get(path)

    assert response.status_code == 404, (
        f"GET {path} answered {response.status_code}. A path this process "
        "serves itself is a miss when nothing claims it, not a client-side route."
    )
    assert not response.headers["content-type"].startswith("text/html")
