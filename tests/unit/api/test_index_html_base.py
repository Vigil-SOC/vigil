"""The served app shell resolves its relative bundle URLs against the context root."""

import re

import pytest

RAW = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <script type="module" crossorigin src="./assets/index-abc.js"></script>
    <link rel="stylesheet" crossorigin href="./assets/index-abc.css">
  </head>
  <body><div id="root"></div></body>
</html>"""


@pytest.mark.parametrize("context_path,href", [("", "/"), ("/vigil", "/vigil/")])
def test_base_is_the_context_root_and_precedes_every_url(context_path, href):
    from services.api.main import inject_index_head

    html = inject_index_head(RAW, context_path)

    base = html.index(f'<base href="{href}">')
    assert base < html.index("<meta charset")
    assert base < min(m.start() for m in re.finditer(r"<(script|link)\b", html))
    assert f'<meta name="vigil-base-path" content="{context_path}">' in html
    assert html.count("<base") == 1


def test_extra_tags_follow_the_base():
    from services.api.main import inject_index_head

    tag = '<meta name="vigil-extension-allowlist" content="https://x">'
    html = inject_index_head(RAW, "", [tag])
    assert html.index("<base") < html.index(tag) < html.index("<script")
