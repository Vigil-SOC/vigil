"""Bifrost capability probe — merge blocker for GH #84 PR-B.

Vigil now routes *all* LLM traffic through Bifrost, including Anthropic
traffic that used to bypass it for extended thinking + native prompt
caching. This script verifies that Bifrost's Anthropic-compatible
passthrough endpoint preserves the Anthropic-native features we depend on
before unifying the routing path.

If any probe fails, the PR must not ship until the upstream Bifrost
behavior is fixed. **Do not add a direct-SDK bypass** — per project
policy the gateway is the single path for LLM traffic.

Usage::

    BIFROST_URL=http://localhost:8080 \\
    ANTHROPIC_API_KEY=sk-ant-... \\
    python scripts/bifrost_capability_probe.py

Exit code 0 → all probes passed; non-zero → one or more features are not
passed through and the unification cannot ship.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Tuple

MODEL = os.getenv("BIFROST_PROBE_MODEL", "claude-sonnet-4-5-20250929")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def _info(msg: str) -> None:
    print(f"       {msg}")


async def _client():
    """Build an Anthropic SDK client pointed at Bifrost's Anthropic passthrough."""
    from anthropic import AsyncAnthropic  # type: ignore

    bifrost_url = os.environ["BIFROST_URL"].rstrip("/")
    api_key = os.environ["ANTHROPIC_API_KEY"]
    return AsyncAnthropic(
        api_key=api_key,
        base_url=f"{bifrost_url}/anthropic",
        timeout=60.0,
    )


async def probe_basic() -> bool:
    """Probe 1 — minimal round-trip through Bifrost's Anthropic endpoint."""
    try:
        client = await _client()
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=32,
            messages=[{"role": "user", "content": "Reply with the single word: ping"}],
        )
        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        )
        if not text.strip():
            _fail("basic round-trip: empty response text")
            return False
        _ok(f"basic round-trip via Bifrost → {MODEL}")
        _info(f"response: {text.strip()[:80]!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        _fail(f"basic round-trip raised: {exc}")
        return False


async def probe_extended_thinking() -> bool:
    """Probe 2 — Bifrost must forward the `thinking` parameter to Anthropic."""
    try:
        client = await _client()
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=2048,
            thinking={"type": "enabled", "budget_tokens": 1024},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Solve step by step: if a train leaves at 3:00 going 60 mph "
                        "and another at 3:30 going 80 mph from the same station in "
                        "the same direction, when does the second catch up?"
                    ),
                }
            ],
        )
        has_thinking_block = any(getattr(b, "type", "") == "thinking" for b in resp.content)
        if not has_thinking_block:
            _fail(
                "extended thinking: response contained no `thinking` block — "
                "Bifrost likely dropped the parameter"
            )
            return False
        _ok("extended thinking preserved through Bifrost")
        return True
    except Exception as exc:  # noqa: BLE001
        _fail(f"extended thinking raised: {exc}")
        return False


async def probe_prompt_caching() -> Tuple[bool, bool]:
    """Probe 3 — Bifrost must preserve `cache_control` on system blocks and
    surface `cache_creation_input_tokens` / `cache_read_input_tokens` in the
    response usage object.
    """
    # A system prompt long enough to beat Anthropic's 1024-token minimum for
    # caching, with a per-run nonce so we always exercise cache *creation*
    # (the prefix is never already-warm from a prior probe invocation).
    nonce = uuid.uuid4().hex
    big_system = (
        f"Probe nonce {nonce}. You are a patient, thorough tutor who explains "
        "scientific processes with rigorous detail and worked examples. " * 80
    )
    system_blocks = [
        {"type": "text", "text": big_system, "cache_control": {"type": "ephemeral"}}
    ]

    try:
        client = await _client()

        # First call — populates the cache.
        r1 = await client.messages.create(
            model=MODEL,
            max_tokens=32,
            system=system_blocks,
            messages=[{"role": "user", "content": "Say: one"}],
        )
        u1 = getattr(r1, "usage", None)
        cc1 = getattr(u1, "cache_creation_input_tokens", None) if u1 else None
        cr1 = getattr(u1, "cache_read_input_tokens", None) if u1 else None

        if cc1 is None and cr1 is None:
            _fail(
                "prompt caching: usage object did not expose "
                "cache_creation_input_tokens / cache_read_input_tokens — "
                "Bifrost is stripping them from the response"
            )
            return False, False

        # Second call — should be a cache hit. Small delay to let Anthropic's
        # prompt cache finish propagating the write from call 1.
        await asyncio.sleep(2)
        r2 = await client.messages.create(
            model=MODEL,
            max_tokens=32,
            system=system_blocks,
            messages=[{"role": "user", "content": "Say: two"}],
        )
        u2 = getattr(r2, "usage", None)
        cr2 = getattr(u2, "cache_read_input_tokens", 0) if u2 else 0

        creation_seen = bool((cc1 or 0) > 0)
        read_seen = bool((cr2 or 0) > 0)

        if not creation_seen:
            _fail(
                f"prompt caching: first call reported no cache_creation_input_tokens "
                f"(got {cc1}) — cache_control likely dropped by Bifrost"
            )
        else:
            _ok(f"first call created cache ({cc1} tokens)")

        if not read_seen:
            _fail(
                f"prompt caching: second call reported no cache_read_input_tokens "
                f"(got {cr2}) — cache is not being hit through Bifrost"
            )
        else:
            _ok(f"second call read cache ({cr2} tokens)")

        return creation_seen, read_seen
    except Exception as exc:  # noqa: BLE001
        _fail(f"prompt caching raised: {exc}")
        return False, False


async def main() -> int:
    if not os.getenv("BIFROST_URL"):
        _fail("BIFROST_URL is not set")
        return 2
    if not os.getenv("ANTHROPIC_API_KEY"):
        _fail("ANTHROPIC_API_KEY is not set")
        return 2

    print(f"Probing Bifrost at {os.environ['BIFROST_URL']} with model {MODEL}\n")

    basic = await probe_basic()
    thinking = await probe_extended_thinking()
    cache_create, cache_read = await probe_prompt_caching()

    print()
    results = {
        "basic round-trip": basic,
        "extended thinking passthrough": thinking,
        "prompt cache creation": cache_create,
        "prompt cache read": cache_read,
    }
    for name, passed in results.items():
        mark = "✅" if passed else "❌"
        print(f"{mark} {name}")

    if all(results.values()):
        print("\nAll probes passed — PR-B unification is safe to ship.")
        return 0

    print(
        "\nOne or more probes failed — do NOT merge PR-B. File an issue on "
        "https://github.com/maximhq/bifrost and hold the unification until upstream fixes it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
