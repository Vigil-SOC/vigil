"""The third-party fault cells (SW I2): L-2.a, L-5.a and I-1.g.

Same contract and stack as config_cells.py. Third parties are faulted at the one
stub (tests/medic_compose/stub.py): the `bifrost` stand-in answers the chat route
with a 401 or a 402, and the `splunk-stub` fails every search. Each verify reads
Vigil (the llm-worker's log, the federation source row), never Medic.

The LLM cells send calls the real way: the gateway's client, run in the backend
container, enqueues `llm_call` for llm-worker. A `provider_id` for a row the cell
creates sends the call down the router path (jobs.py `_maybe_dispatch_via_router`),
the only one through gateway_retry. The llm-worker log persists across runs, so
every check counts lines before inject and wants new ones.
"""

from __future__ import annotations

import json

from tests.medic_eval.inject.config_cells import _expect
from tests.medic_eval.inject.contract import Cell
from tests.medic_eval.inject.stack import Stack

# ---- the LLM cells ----------------------------------------------------------

PROVIDER = "eval-stub-provider"
CALLS = 3  # llm-provider-key-unusable.yaml's `calls`
CHAT = "/v1/chat/completions"

# Submit n calls through the gateway and print how each ended: "error" for an
# error dict, "ok" for an answer with content (the router path's result carries no
# "type"), "none" for no answer.
SUBMIT = r"""
import asyncio, json, sys
from core.llm.gateway.gateway import get_llm_gateway

async def main(provider_id, n):
    gw = await get_llm_gateway()
    results = await asyncio.gather(
        *[gw.submit("ping", provider_id=provider_id, model="eval-model", timeout=60) for _ in range(int(n))]
    )
    print(json.dumps([
        "error" if (r or {}).get("type") == "error" else "ok" if (r or {}).get("content") else "none"
        for r in results
    ]))
    await gw.close()

asyncio.run(main(*sys.argv[1:3]))
"""


def _stub_mode(s: Stack, mode: str) -> None:
    """What the bifrost stub answers chat with; "" is its default, a completion."""
    s.py(
        "bifrost",
        "import pathlib, sys; p = pathlib.Path('/tmp/bifrost-mode'); "
        "p.write_text(sys.argv[1]) if sys.argv[1] else p.unlink(missing_ok=True)",
        mode,
    )


def _stub_answers(s: Stack) -> int:
    """The status the stub gives a chat call right now."""
    return s.http("POST", "bifrost", 8080, CHAT, body="{}")[0]


def _calls(s: Stack, n: int) -> list[str]:
    return json.loads(s.py("backend", SUBMIT, PROVIDER, str(n)))


def _provider(s: Stack) -> str:
    return s.sql(
        f"SELECT count(*) FROM llm_provider_configs WHERE provider_id = '{PROVIDER}'"
    )


def _add_provider(s: Stack) -> None:
    # Any type the router sends to Bifrost; no ai_model_configs row points at it.
    s.sql(
        "INSERT INTO llm_provider_configs (provider_id, provider_type, name, default_model) "
        f"VALUES ('{PROVIDER}', 'openai', 'Eval stub provider', 'eval-model') ON CONFLICT DO NOTHING"
    )


def _count(s: Stack, line: str) -> int:
    return s.logs("llm-worker").count(line)


def _llm_inject(s: Stack, cause: str, mode: str, line: str) -> None:
    if cause in s.active:
        return
    _stub_mode(s, mode)
    _add_provider(s)
    s.marks[cause] = _count(s, line)
    _expect(_calls(s, CALLS), ["error"] * CALLS, "how the calls ended")
    s.active.add(cause)


def _llm_revert(s: Stack, cause: str) -> None:
    if cause not in s.active and _provider(s) == "0" and _stub_answers(s) == 200:
        return
    # A call that gets through is what clears the budget latch (jobs.py), so make one.
    _stub_mode(s, "")
    _add_provider(s)
    _expect(_calls(s, 1), ["ok"], "the recovery call")
    s.sql(f"DELETE FROM llm_provider_configs WHERE provider_id = '{PROVIDER}'")
    s.active.discard(cause)


def _llm_clean(s: Stack) -> None:
    _expect(_provider(s), "0", "the provider row")
    _expect(_stub_answers(s), 200, "bifrost stub's chat answer")


# ---- L-2.a: the provider rejects the key ------------------------------------
# Bifrost answers 401 `invalid_api_key`; through_gateway does not retry it, and
# llm_call logs it and returns an error dict (so ARQ does not re-run it): one line
# per call. The traceback also names AuthenticationError, so the log line's own
# prefix is counted.

AUTH_LINE = "llm_call failed (returning error dict): AuthenticationError:"


def l2a_inject(s: Stack) -> None:
    _llm_inject(s, "L-2.a", "401", AUTH_LINE)


def l2a_revert(s: Stack) -> None:
    _llm_revert(s, "L-2.a")


def l2a_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        new = _count(s, AUTH_LINE) - s.marks["L-2.a"]
        assert new >= CALLS, f"{new} new {AUTH_LINE!r} line(s) in llm-worker, want {CALLS}"
    else:
        _llm_clean(s)


# ---- L-5.a: the virtual key's budget is spent -------------------------------
# Bifrost answers 402 with no "team" or "customer" in it, so the tier is
# virtual_key. report_outage logs once per outage: the line comes on the first
# failure after a recovery, and the revert's successful call re-arms it.

BUDGET_LINE = "LLM budget spent (virtual_key tier)"
BUDGET_OK = "LLM budget available (virtual_key tier); recovered after"


def l5a_inject(s: Stack) -> None:
    if "L-5.a" not in s.active:
        s.marks["L-5.a-ok"] = _count(s, BUDGET_OK)
    _llm_inject(s, "L-5.a", "402", BUDGET_LINE)


def l5a_revert(s: Stack) -> None:
    _llm_revert(s, "L-5.a")


def l5a_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        assert _count(s, BUDGET_LINE) > s.marks["L-5.a"], f"no new {BUDGET_LINE!r} line"
    else:
        _llm_clean(s)
        # The latch's own clear: a routed call got through after the failures.
        assert _count(s, BUDGET_OK) > s.marks["L-5.a-ok"], f"no new {BUDGET_OK!r} line"


# ---- I-1.g: every Splunk search fails ---------------------------------------
# splunk-stub logs in and fails the search, so the adapter raises on its first
# query and the runner counts the tick as an error. Federation (global and the
# "splunk" source) is on at the API's 10 s floor and the daemon is recreated: the
# adapter caches its SplunkService and loops are built at start. The runner backs
# off 2x, 4x, 8x the interval, so three errors take about a minute. Revert deletes
# the source row, which resets consecutive_errors for the next run.

SPLUNK = "splunk"
SPLUNK_STUB = "http://splunk-stub:8089"
FED_INTERVAL = 10
SPLUNK_BASELINE = "false|false|false|"


def _splunk_state(s: Stack) -> str:
    """global | source enabled | integration enabled | consecutive_errors ("" without a row)."""
    return s.sql(
        "SELECT concat_ws('|', "
        "coalesce((SELECT value->>'enabled' FROM system_config WHERE key = 'federation.settings'), 'false'), "
        f"coalesce((SELECT enabled::text FROM federation_sources WHERE source_id = '{SPLUNK}'), 'false'), "
        f"coalesce((SELECT enabled::text FROM integration_configs WHERE integration_id = '{SPLUNK}'), 'false'), "
        f"coalesce((SELECT consecutive_errors::text FROM federation_sources WHERE source_id = '{SPLUNK}'), ''))"
    )


def _splunk_errors(s: Stack) -> int:
    status, body = s.api("GET", "/api/federation/health")
    _expect(status, 200, "GET /api/federation/health")
    return next(
        (r["consecutive_errors"] for r in body["sources"] if r["source_id"] == SPLUNK), 0
    )


def i1g_inject(s: Stack) -> None:
    if "I-1.g" in s.active:
        return
    s.set_integration(
        SPLUNK,
        {"server_url": SPLUNK_STUB, "username": "eval", "password": "eval"},
        True,
    )
    status, body = s.api("PUT", "/api/federation/settings", {"enabled": True})
    assert status == 200, (status, body)
    status, body = s.api(
        "PATCH",
        f"/api/federation/sources/{SPLUNK}",
        {"enabled": True, "interval_seconds": FED_INTERVAL},
    )
    assert status == 200, (status, body)
    s.recreate("soc-daemon")
    s.wait_daemon()
    s.active.add("I-1.g")


def i1g_revert(s: Stack) -> None:
    if "I-1.g" not in s.active and _splunk_state(s) == SPLUNK_BASELINE:
        return
    s.set_integration(SPLUNK, {}, False)
    status, body = s.api("PUT", "/api/federation/settings", {"enabled": False})
    assert status == 200, (status, body)
    status, body = s.api("PATCH", f"/api/federation/sources/{SPLUNK}", {"enabled": False})
    assert status == 200, (status, body)
    s.recreate("soc-daemon")  # nothing polls Splunk now; the next run starts at zero errors
    s.wait_daemon()
    s.sql(f"DELETE FROM federation_sources WHERE source_id = '{SPLUNK}'")
    s.active.discard("I-1.g")


def i1g_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        s.wait(lambda: _splunk_errors(s), lambda n: n >= 3, timeout=180)
    else:
        _expect(_splunk_state(s), SPLUNK_BASELINE, "federation and splunk settings")
        _expect(_splunk_errors(s), 0, "splunk consecutive_errors")


CELLS = (
    Cell(
        "L-2.a",
        2,
        # llm-provider-key-unusable.yaml: count over 15m, then `for: 2m`.
        900 + 120,
        l2a_inject,
        l2a_verify,
        l2a_revert,
        (
            "bifrost stub answers 401 invalid_api_key on chat completions",
            f"submit {CALLS} routed LLM calls through the gateway to llm-worker",
        ),
        (
            "bifrost stub answers a completion again",
            "one routed LLM call succeeds; delete the provider row",
        ),
    ),
    Cell(
        "L-5.a",
        2,
        # llm-budget-exhausted.yaml: a latch with no window, then `for: 2m`.
        120,
        l5a_inject,
        l5a_verify,
        l5a_revert,
        (
            "bifrost stub answers 402 with a virtual-key budget body",
            f"submit {CALLS} routed LLM calls through the gateway to llm-worker",
        ),
        (
            "bifrost stub answers a completion again",
            "one routed LLM call succeeds (clears the latch); delete the provider row",
        ),
    ),
    Cell(
        "I-1.g",
        2,
        # ingest-source-polls-failing.yaml: `for: 10m`, no window.
        600,
        i1g_inject,
        i1g_verify,
        i1g_revert,
        (
            "enable the splunk integration at a splunk-stub that fails every search",
            "enable federation and its splunk source",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        (
            "disable federation, the splunk source and the integration; delete the source row",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        # ingest-source-polls-failing.yaml is mode I-2, not the cause's prefix.
        mode_override="I-2",
    ),
)
