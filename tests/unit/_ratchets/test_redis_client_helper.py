"""Every Redis client and URL in core/ and services/ comes from core/redis_client.py.

That is where REDIS_PASSWORD is applied; a call site that reads ``redis_url``
or builds a client from its own URL connects without it. A caller may still
build its own client (a loop or a sync client of its own), but only from
``effective_redis_url()``, directly or through a name assigned from it.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "core" / "redis_client.py"
GATEWAY = ROOT / "core" / "llm" / "gateway" / "gateway.py"

URL_TAKERS = {"from_url", "from_dsn"}  # first argument is the URL
BANNED_CALLS = {"Redis", "StrictRedis", "RedisCluster", "Sentinel", "create_pool"}
BANNED_NAMES = {"RedisSettings", "ArqRedis"}


def _name(node):
    return getattr(node, "attr", None) or getattr(node, "id", None)


def _is_helper_call(node):
    return isinstance(node, ast.Call) and _name(node.func) == "effective_redis_url"


def _offences(path: Path, root: Path = ROOT):
    tree = ast.parse(path.read_text(), filename=str(path))
    # Names (and self.attributes) that hold the helper's URL.
    from_helper = {
        _name(target)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _is_helper_call(node.value)
        for target in node.targets
    }

    def sourced(value):
        return _is_helper_call(value) or (
            isinstance(value, (ast.Name, ast.Attribute)) and _name(value) in from_helper
        )

    for node in ast.walk(tree):
        what = None
        if isinstance(node, ast.Call):
            name = _name(node.func)
            url_arg = (
                node.args[0]
                if node.args
                else next(
                    (kw.value for kw in node.keywords if kw.arg in ("url", "dsn")), None
                )
            )
            if name in URL_TAKERS and url_arg is not None and not sourced(url_arg):
                what = f"{name}( without effective_redis_url()"
            elif name in BANNED_CALLS:
                what = f"{name}("
            for kw in node.keywords:
                if kw.arg == "storage_uri" and not sourced(kw.value):
                    what = "storage_uri= without effective_redis_url()"
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "connection"
                    and not sourced(value)
                ):
                    what = '"connection": without effective_redis_url()'
        elif isinstance(node, ast.Attribute) and node.attr == "redis_url":
            what = ".redis_url"
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            what = node.id
        elif isinstance(node, ast.alias) and node.name.split(".")[-1] in BANNED_NAMES:
            what = node.name
        if what is None:
            continue
        # The gateway hands ARQ's create_pool the RedisSettings it builds from
        # the helper's URL; ARQ, not Vigil, makes the client.
        if path == GATEWAY and what in {"create_pool(", *BANNED_NAMES}:
            continue
        yield f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')}: {what}"


def test_redis_connections_go_through_the_helper():
    found = [
        hit
        for tree in ("core", "services")
        for path in sorted((ROOT / tree).rglob("*.py"))
        if path != HELPER and "node_modules" not in path.parts
        for hit in _offences(path)
    ]
    assert not found, "Build Redis URLs with core.redis_client:\n" + "\n".join(found)


def test_the_ratchet_catches_a_hand_built_client(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import redis\n"
        "from core.config import get_settings\n"
        "c = redis.from_url(get_settings().redis_url)\n"
        "d = redis.from_url(url='redis://x')\n"
        "q = Queue('x', {'connection': 'redis://localhost:6379/0'})\n"
    )
    hits = list(_offences(bad, tmp_path))
    assert len(hits) == 4, hits
