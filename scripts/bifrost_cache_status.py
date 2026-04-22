"""Report Bifrost cache configuration status (GH #84 follow-up).

Reality check: Bifrost v1.4.23's caching layer is ``semantic_cache``, a
plugin that requires a vector store + embedding provider — not the
simple exact-hash cache this repo's PR-B originally planned around. It's
configured through the Bifrost UI at http://localhost:8080 rather than
the (now-rejected) ``cache`` block in ``docker/bifrost/config.json``.

This script is diagnostic only. It hits Bifrost's local API and
reports:
  - whether Bifrost exposes ``is_cache_connected: true``
  - whether the ``semantic_cache`` plugin is registered and enabled
  - hint text for enabling it via the UI

Vigil's primary cost-reduction layer is Anthropic native prompt caching
(GH #84 PR-C). Bifrost's semantic cache is optional gravy on top — use
this script to check whether it's live after manual UI setup.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


BIFROST_URL = os.getenv("BIFROST_URL", "http://localhost:8080").rstrip("/")


def _get(path: str) -> tuple[int, dict | str]:
    req = urllib.request.Request(f"{BIFROST_URL}{path}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                return resp.status, json.loads(body)
            return resp.status, body[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def main() -> int:
    print(f"Probing Bifrost at {BIFROST_URL}\n")

    status, health = _get("/health")
    if status != 200:
        print(f"[FAIL] Bifrost /health returned {status}: {health}")
        return 2

    status, config = _get("/api/config")
    if status != 200 or not isinstance(config, dict):
        print(f"[FAIL] /api/config returned {status}")
        return 2
    connected = config.get("is_cache_connected")

    status, plugins = _get("/api/plugins")
    plugin_names = []
    if status == 200 and isinstance(plugins, dict):
        plugin_names = [p.get("name") for p in plugins.get("plugins", [])]

    semantic_cache = None
    if "semantic_cache" in plugin_names:
        status, sc = _get("/api/plugins/semantic_cache")
        if status == 200 and isinstance(sc, dict):
            semantic_cache = sc

    # Report
    print(f"is_cache_connected: {connected}")
    print(f"registered plugins: {plugin_names or '(none)'}")
    if semantic_cache is not None:
        print(f"semantic_cache.enabled: {semantic_cache.get('enabled')}")
        print(f"semantic_cache.config: {json.dumps(semantic_cache.get('config'), indent=2)}")
    else:
        print("semantic_cache plugin: not registered")

    print()
    if connected:
        print("✅ Bifrost cache is live — duplicate/semantically-similar")
        print("   requests served from Bifrost's vector store without")
        print("   round-tripping to the upstream LLM.")
        return 0

    print("ℹ️  Bifrost cache is not configured.")
    print("   Anthropic native prompt caching (GH #84 PR-C) is still")
    print("   active and is the primary cost-reduction layer — ~90% on")
    print("   cached input tokens for repeated system + tool prefixes.")
    print()
    print("   To enable Bifrost's semantic cache on top of that:")
    print(f"     1. Open the Bifrost UI at {BIFROST_URL}")
    print("     2. Configure a vector store (Redis recommended — reuse")
    print("        redis:6379 with a separate DB number to isolate from")
    print("        the ARQ queue on DB 0).")
    print("     3. Configure an embedding provider (OpenAI text-embedding-3-small")
    print("        or a local Ollama model — configured via the same UI).")
    print("     4. Enable the semantic_cache plugin.")
    print("     5. Re-run this script to confirm is_cache_connected=true.")
    print()
    print("   Note: Bifrost v1.4.23 rejects a top-level 'cache' block in")
    print("   docker/bifrost/config.json — settings live in the SQLite")
    print("   store backing the UI. Expect that gap to close in a future")
    print("   Bifrost release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
