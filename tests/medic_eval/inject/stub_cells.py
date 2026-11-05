"""The third-party fault cells (SW I2): L-2.a, L-5.a and I-1.g.

Same contract and stack as config_cells.py; each verify reads Vigil (the llm-worker's
log, the federation health API, the database), never Medic. The vendors are the one
stub (tests/medic_compose/stub.py): the `bifrost` role plays the LLM gateway and the
`splunk` role the Splunk the daemon polls, so no vendor account is needed.

The L cells send calls the real way: the backend's LLM gateway client enqueues an ARQ
job with a provider_id for a row the cell creates, which sends the llm-worker down the
router path (services/worker/jobs.py) to the bifrost stub.
"""

from __future__ import annotations

import json

from tests.medic_eval.inject.config_cells import _eventually, _expect
from tests.medic_eval.inject.contract import Cell
from tests.medic_eval.inject.stack import Stack

# ---- L-2.a and L-5.a: the gateway refuses every call -------------------------
# The llm-worker's log outlives a cell and each cell runs twice on one stack, so
# a verify counts the matching lines added since inject, never the log's total.

PROVIDER = "eval-llm-provider"
CALLS = 3  # llm-provider-key-unusable.yaml's `calls`
# Any type the router sends to Bifrost; no ai_model_configs row names it.
PROVIDER_ROW = (
    "INSERT INTO llm_provider_configs (provider_id, provider_type, name, default_model) "
    f"VALUES ('{PROVIDER}', 'openai', 'eval provider', 'eval-model') ON CONFLICT DO NOTHING"
)
# Sends n calls through the backend's gateway client; prints each result's error ("" if none).
SEND = r"""
import asyncio, json, sys
from core.llm.gateway.gateway import get_llm_gateway

async def main():
    gw = await get_llm_gateway()
    out = await asyncio.gather(*(
        gw.submit(f"eval call {i}", model="eval-model", timeout=60, provider_id=sys.argv[1])
        for i in range(int(sys.argv[2]))
    ))
    print(json.dumps([(r or {}).get("error", "") for r in out]))

asyncio.run(main())
"""
AUTH_LINE = "llm_call failed (returning error dict): AuthenticationError:"
BUDGET_LINE = "LLM budget spent (virtual_key tier)"
RECOVERED_LINE = "LLM budget available (virtual_key tier); recovered after"


def _bifrost_fault(s: Stack, status: str) -> None:
    """What the stub answers a chat completion with; "" is its default, a good one."""
    s.py(
        "bifrost",
        "import pathlib, sys; p = pathlib.Path('/tmp/bifrost-fault'); "
        "p.write_text(sys.argv[1]) if sys.argv[1] else p.unlink(missing_ok=True)",
        status,
    )


def _send(s: Stack, n: int) -> list[str]:
    out = s.py("backend", SEND, PROVIDER, str(n))
    return json.loads(out.splitlines()[-1])


def _lines(s: Stack, needle: str) -> int:
    return sum(needle in line for line in s.logs("llm-worker").splitlines())


def _provider_rows(s: Stack) -> str:
    return s.sql(f"SELECT count(*) FROM llm_provider_configs WHERE provider_id = '{PROVIDER}'")


def _l_inject(s: Stack, cause: str, status: str, needle: str) -> None:
    if cause in s.active:
        return
    s.sql(PROVIDER_ROW)
    _bifrost_fault(s, status)
    s.marks[cause] = _lines(s, needle)
    s.active.add(cause)
    errors = _send(s, CALLS)
    assert all(errors), f"calls the fault should have refused: {errors}"


def _l_revert(s: Stack, cause: str) -> None:
    # A call that gets through is what clears the budget rule's latch, so make one.
    _bifrost_fault(s, "")
    if cause in s.active:
        s.sql(PROVIDER_ROW)
        _expect(_send(s, 1), [""], "the call after the fault was cleared")
    s.sql(f"DELETE FROM llm_provider_configs WHERE provider_id = '{PROVIDER}'")
    s.active.discard(cause)


def _l_verify(s: Stack, faulted: bool, cause: str, needle: str, want: int, clear=None) -> None:
    if faulted:
        s.wait(
            lambda: _lines(s, needle) - s.marks[cause],
            lambda n: n >= want,
            timeout=90,
        )
        return
    _expect(_provider_rows(s), "0", "provider rows left behind")
    if clear:
        s.wait(lambda: _lines(s, clear), lambda n: n > s.marks[cause + ".clear"], timeout=90)


# ---- L-2.a: the provider rejects the key ------------------------------------
# A 401 is not retried; llm_call logs one line per call and returns an error dict, so
# ARQ does not run it again.


def l2a_inject(s: Stack) -> None:
    _l_inject(s, "L-2.a", "401", AUTH_LINE)


def l2a_revert(s: Stack) -> None:
    _l_revert(s, "L-2.a")


def l2a_verify(s: Stack, faulted: bool) -> None:
    _l_verify(s, faulted, "L-2.a", AUTH_LINE, CALLS)
    if not faulted:  # nothing past the calls inject sent
        _expect(_lines(s, AUTH_LINE) - s.marks["L-2.a"], CALLS, "new AuthenticationError lines")


# ---- L-5.a: the virtual-key budget is spent ----------------------------------
# report_outage logs the budget line on the first refusal after a recovery only, so one
# new line is the fault. The revert's good call logs the recovery and re-arms it.


def l5a_inject(s: Stack) -> None:
    if "L-5.a" not in s.active:
        s.marks["L-5.a.clear"] = _lines(s, RECOVERED_LINE)
    _l_inject(s, "L-5.a", "402", BUDGET_LINE)


def l5a_revert(s: Stack) -> None:
    _l_revert(s, "L-5.a")


def l5a_verify(s: Stack, faulted: bool) -> None:
    _l_verify(s, faulted, "L-5.a", BUDGET_LINE, 1, None if faulted else RECOVERED_LINE)


# ---- I-1.g: every Splunk search fails ----------------------------------------
# Federation is on for `splunk`, the integration's URL is the splunk stub (a login
# works, the search is refused) and the daemon is recreated: the adapter caches its
# client and the poll loops are built at start. consecutive_errors only ever resets on
# a success, so revert zeroes it, else run 2 would read run 1's count.

SPLUNK = "splunk"
SPLUNK_STUB = "http://splunk-stub:8089"
SPLUNK_BASELINE = "false|false|false|0"
FED_INTERVAL = 10  # the API's floor, so errors pile up within seconds
MIN_ERRORS = 3  # ingest-source-polls-failing.yaml's `min_errors`


def _splunk_state(s: Stack) -> str:
    """federation enabled | source enabled | integration enabled | consecutive_errors."""
    src = "SELECT {} FROM federation_sources WHERE source_id = 'splunk'"
    return s.sql(
        "SELECT concat_ws('|', "
        "coalesce((SELECT value->>'enabled' FROM system_config WHERE key = 'federation.settings'), 'false'), "
        f"coalesce(({src.format('enabled::text')}), 'false'), "
        f"coalesce((SELECT enabled::text FROM integration_configs WHERE integration_id = '{SPLUNK}'), 'false'), "
        f"coalesce(({src.format('consecutive_errors::text')}), '0'))"
    )


def _errors(s: Stack) -> int:
    """consecutive_errors for splunk as the federation health API reports it."""
    status, body = s.api("GET", "/api/federation/health")
    _expect(status, 200, "GET /api/federation/health")
    return next((r["consecutive_errors"] for r in body["sources"] if r["source_id"] == SPLUNK), 0)


def _splunk(s: Stack, on: bool) -> None:
    config = {"server_url": SPLUNK_STUB, "username": "eval", "password": "eval"}
    s.set_integration(SPLUNK, config if on else {}, on)
    status, body = s.api("PUT", "/api/federation/settings", {"enabled": on})
    assert status == 200, (status, body)
    patch = {"enabled": on, "interval_seconds": FED_INTERVAL}
    status, body = s.api("PATCH", f"/api/federation/sources/{SPLUNK}", patch)
    assert status == 200, (status, body)
    s.recreate("soc-daemon")
    s.wait_daemon()
    if not on:  # the old daemon is gone, so nothing can add to the count after this
        s.sql("UPDATE federation_sources SET consecutive_errors = 0, last_error = NULL, "
              f"last_error_kind = NULL WHERE source_id = '{SPLUNK}'")


def i1g_inject(s: Stack) -> None:
    if "I-1.g" not in s.active:
        _splunk(s, True)
        s.active.add("I-1.g")


def i1g_revert(s: Stack) -> None:
    if "I-1.g" in s.active or _splunk_state(s) != SPLUNK_BASELINE:
        _splunk(s, False)
    s.active.discard("I-1.g")


def i1g_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        _eventually(s, lambda: _errors(s) >= MIN_ERRORS, True, f"splunk consecutive_errors >= {MIN_ERRORS}")
        return
    _expect(_splunk_state(s), SPLUNK_BASELINE, "federation, splunk source and integration")
    assert "Splunk query failed" not in s.logs("soc-daemon"), "the recreated daemon still polls Splunk"


CELLS = (
    # detect_within_s is the rule's windows, then its `for:`.
    # llm-provider-key-unusable.yaml: count over 15m, for: 2m.
    Cell(
        "L-2.a",
        2,
        900 + 120,
        l2a_inject,
        l2a_verify,
        l2a_revert,
        (
            "the bifrost stub answers 401 invalid_api_key to chat completions",
            f"submit {CALLS} LLM calls through the gateway with a provider_id",
        ),
        (
            "the bifrost stub answers a good completion again",
            "one routed call succeeds; delete the provider row",
        ),
    ),
    # llm-budget-exhausted.yaml: a latch with no window, for: 2m.
    Cell(
        "L-5.a",
        2,
        120,
        l5a_inject,
        l5a_verify,
        l5a_revert,
        (
            "the bifrost stub answers 402 with a virtual-key budget body",
            "submit LLM calls through the gateway with a provider_id",
        ),
        (
            "the bifrost stub answers a good completion again",
            "one routed call succeeds (clears the latch); delete the provider row",
        ),
    ),
    # ingest-source-polls-failing.yaml: for: 10m. Its mode is I-2, not the cause's I-1.
    Cell(
        "I-1.g",
        2,
        600,
        i1g_inject,
        i1g_verify,
        i1g_revert,
        (
            "point the splunk integration at a stub that logs in but refuses every search",
            "enable federation and its splunk source",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        (
            "disable federation, the splunk source and the integration; zero its error count",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        mode_override="I-2",
    ),
)
