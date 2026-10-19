"""The served index.html pins relative asset URLs to the context root.

The Vite build emits ``./assets/...``; without a ``<base>`` those resolve under
the current URL (``/cases/abc`` -> ``/cases/assets/...``) and the app shell
comes back instead of the bundle, leaving the page blank.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

from services.api.main import inject_index_head  # noqa: E402

pytestmark = pytest.mark.unit

RAW = (
    "<!doctype html><html><head>\n"
    '<meta charset="UTF-8" />\n'
    '<script type="module" crossorigin src="./assets/index-abc.js"></script>\n'
    '<link rel="stylesheet" href="./assets/index-abc.css">\n'
    "</head><body></body></html>"
)


@pytest.mark.parametrize(
    "context_path, href", [("", "/"), ("/vigil", "/vigil/")]
)
def test_base_precedes_every_url_bearing_tag(context_path, href):
    html = inject_index_head(RAW, context_path, ['<meta name="x" content="y">'])

    base = html.index(f'<base href="{href}">')
    assert base < html.index("<meta charset")
    assert base < html.index('<meta name="x"')
    assert base < html.index("<script")
    assert base < html.index("<link")
    assert html.count("<base ") == 1
