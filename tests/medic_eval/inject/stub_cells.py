"""The third-party fault cells (SW I2): L-2.a, L-5.a and I-1.g.

Same contract and stack as config_cells.py; each verify reads Vigil (the llm-worker's
log, the federation health API, the database), never Medic. The fault is made at the
stubs in tests/medic_compose/stub.py: `bifrost` answers the worker's chat calls with an
error status written to /tmp/bifrost-mode, and `splunk-stub` fails every search.

The llm-worker's log persists across runs, so each cell counts the matching lines
before it injects and needs new ones.
"""

from __future__ import annotations

import json

from tests.medic_eval.inject.config_cells import _eventually, _expect
from tests.medic_eval.inject.contract import Cell
from tests.medic_eval.inject.stack import Stack

# ---- L-2 / L-5: LLM calls through the llm-worker's router path ---------------
# A provider row the cell creates makes llm_call take _maybe_dispatch_via_router,
# the only path through gateway_retry. Any type the router sends to Bifrost will do.
# No ai_model_configs row names it (that FK is ON DELETE RESTRICT), so revert deletes it.

PROVIDER = "eval-stub-provider"
MODE_FILE = "/tmp/bifrost-mode"
AUTH_LINE = "llm_call failed (returning error dict): AuthenticationError:"
SPENT_LINE = "LLM budget spent (virtual_key tier)"
AVAILABLE_LINE = "LLM budget available (virtual_key tier)"
CALLS = 3  # the L-2.a rule's `calls`

# Submits n calls the real way (the gateway's ARQ enqueue) and prints each result's type.
SUBMIT = """
import asyncio, json, sys
from core.llm.gateway.gateway import get_llm_gateway

async def main(n, provider_id):
    gw = await get_llm_gateway()
    rs = await asyncio.gather(*[
        gw.submit(f"eval call {i}", model="eval-model", provider_id=provider_id, timeout=60)
        for i in range(int(n))
    ])
    print(json.dumps([r["type"] for r in rs]))

asyncio.run(main(*sys.argv[1:3]))
"""
# Writes (or, when empty, removes) the stub's answer mode.
SET_MODE = (
    "import pathlib, sys; p = pathlib.Path(sys.argv[1]); "
    "p.write_text(sys.argv[2]) if sys.argv[2] else p.unlink(missing_ok=True)"
)
GET_MODE = "import pathlib, sys; p = pathlib.Path(sys.argv[1]); print(p.read_text() if p.exists() else '')"


def _lines(s: Stack, text: str) -> int:
    return s.logs("llm-worker").count(text)


def _provider_rows(s: Stack) -> str:
    return s.sql(f"SELECT count(*) FROM llm_provider_configs WHERE provider_id = '{PROVIDER}'")


def _stub_mode(s: Stack) -> str:
    return s.py("bifrost", GET_MODE, MODE_FILE)


def _submit(s: Stack, n: int) -> list[str]:
    return json.loads(s.py("backend", SUBMIT, str(n), PROVIDER))


def _llm_inject(s: Stack, cause: str, status: str, watched: str) -> None:
    if cause in s.active:
        return
    s.marks[cause] = _lines(s, watched)
    s.py("bifrost", SET_MODE, MODE_FILE, status)
    s.sql(
        "INSERT INTO llm_provider_configs (provider_id, provider_type, name, default_model) "
        f"VALUES ('{PROVIDER}', 'openai', 'eval stub provider', 'eval-model') ON CONFLICT DO NOTHING"
    )
    # Every call is refused; the worker returns an error dict rather than raising.
    results = _submit(s, CALLS)
    _expect(results, ["error"] * CALLS, "results of the calls sent")
    s.active.add(cause)


def _llm_revert(s: Stack, cause: str) -> None:
    if cause not in s.active and _stub_mode(s) == "" and _provider_rows(s) == "0":
        return
    s.py("bifrost", SET_MODE, MODE_FILE, "")
    # A call that gets through is what clears the budget latch.
    if _provider_rows(s) == "0":
        s.sql(
            "INSERT INTO llm_provider_configs (provider_id, provider_type, name, default_model) "
            f"VALUES ('{PROVIDER}', 'openai', 'eval stub provider', 'eval-model')"
        )
    _expect(_submit(s, 1), ["text"], "result of the call after revert")
    s.sql(f"DELETE FROM llm_provider_configs WHERE provider_id = '{PROVIDER}'")
    s.active.discard(cause)


def _llm_clean(s: Stack) -> None:
    _expect(_stub_mode(s), "", "bifrost stub mode")
    _expect(_provider_rows(s), "0", "provider rows")


# ---- L-2.a: the provider rejects the key ------------------------------------
# 401 with invalid_api_key is not retried, so each call logs one line. Needs the
# rule's `calls` new lines.


def l2a_inject(s: Stack) -> None:
    _llm_inject(s, "L-2.a", "401", AUTH_LINE)


def l2a_revert(s: Stack) -> None:
    _llm_revert(s, "L-2.a")


def l2a_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        _eventually(
            s,
            lambda: _lines(s, AUTH_LINE) - s.marks["L-2.a"] >= CALLS,
            True,
            f"new `{AUTH_LINE}` lines",
        )
    else:
        _llm_clean(s)


# ---- L-5.a: the budget is spent ---------------------------------------------
# 402 becomes BudgetExceeded; report_outage logs the line on the first failure after
# a recovery, and the revert's successful call is that recovery (so run 2 logs it again).


def l5a_inject(s: Stack) -> None:
    if "L-5.a" not in s.active:
        s.marks["L-5.a.available"] = _lines(s, AVAILABLE_LINE)
    _llm_inject(s, "L-5.a", "402", SPENT_LINE)


def l5a_revert(s: Stack) -> None:
    _llm_revert(s, "L-5.a")


def l5a_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        _eventually(
            s,
            lambda: _lines(s, SPENT_LINE) - s.marks["L-5.a"] >= 1,
            True,
            f"new `{SPENT_LINE}` line",
        )
    else:
        _llm_clean(s)
        # The line the rule's latch clears on.
        _eventually(
            s,
            lambda: _lines(s, AVAILABLE_LINE) > s.marks["L-5.a.available"],
            True,
            f"new `{AVAILABLE_LINE}` line",
        )


# ---- I-1.g: every Splunk search fails ---------------------------------------
# Federation is on for `splunk`, the integration points at splunk-stub, which logs in
# and fails the search; the daemon is recreated because the adapter caches its client
# and the poll loops are built at start. Failures back off (10 s, 20 s, 40 s), so the
# third lands about 30 s in. Revert also zeroes the error count: disabling a source
# keeps it, and a second run would then pass verify before it polled.

SPLUNK = "splunk"
SPLUNK_STUB = "http://splunk-stub:8089"
SPLUNK_CONFIG = {"server_url": SPLUNK_STUB, "username": "eval", "password": "eval-password"}
FED_INTERVAL = 10  # the API's floor
MIN_ERRORS = 3  # the rule's min_errors
SPLUNK_BASELINE = "false|false|0|false"


def _splunk_state(s: Stack) -> str:
    """global | source enabled | consecutive_errors | splunk integration enabled."""
    src = "SELECT {} FROM federation_sources WHERE source_id = 'splunk'"
    return s.sql(
        "SELECT concat_ws('|', "
        "coalesce((SELECT value->>'enabled' FROM system_config WHERE key = 'federation.settings'), 'false'), "
        f"coalesce(({src.format('enabled::text')}), 'false'), "
        f"coalesce(({src.format('consecutive_errors::text')}), '0'), "
        "coalesce((SELECT enabled::text FROM integration_configs WHERE integration_id = 'splunk'), 'false'))"
    )


def _federation_splunk(s: Stack, on: bool) -> None:
    s.set_integration(SPLUNK, SPLUNK_CONFIG if on else {}, on)
    status, body = s.api("PUT", "/api/federation/settings", {"enabled": on})
    assert status == 200, (status, body)
    status, body = s.api(
        "PATCH",
        "/api/federation/sources/splunk",
        {"enabled": on, "interval_seconds": FED_INTERVAL},
    )
    assert status == 200, (status, body)
    s.recreate("soc-daemon")
    s.wait_daemon()


def _splunk_errors(s: Stack) -> int:
    status, body = s.api("GET", "/api/federation/health")
    _expect(status, 200, "GET /api/federation/health")
    return max(
        (r["consecutive_errors"] for r in body["sources"] if r["source_id"] == SPLUNK),
        default=0,
    )


def i1g_inject(s: Stack) -> None:
    if "I-1.g" in s.active:
        return
    _federation_splunk(s, True)
    s.active.add("I-1.g")


def i1g_revert(s: Stack) -> None:
    if "I-1.g" in s.active or _splunk_state(s) != SPLUNK_BASELINE:
        _federation_splunk(s, False)
        s.sql("UPDATE federation_sources SET consecutive_errors = 0, last_error = NULL, last_error_kind = NULL WHERE source_id = 'splunk'")
    s.active.discard("I-1.g")


def i1g_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        s.wait(
            lambda: _splunk_errors(s),
            lambda n: n >= MIN_ERRORS,
            timeout=150,
        )
    else:
        _expect(_splunk_state(s), SPLUNK_BASELINE, "federation and splunk settings")


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
            f"send {CALLS} LLM calls through the gateway for a provider row the cell creates",
        ),
        (
            "bifrost stub answers completions again, one call succeeds",
            "delete the provider row",
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
            "bifrost stub answers 402 with a virtual-key budget body on chat completions",
            f"send {CALLS} LLM calls through the gateway for a provider row the cell creates",
        ),
        (
            "bifrost stub answers completions again, one call succeeds",
            "delete the provider row",
        ),
    ),
    Cell(
        "I-1.g",
        2,
        # ingest-source-polls-failing.yaml: `for: 10m`.
        600,
        i1g_inject,
        i1g_verify,
        i1g_revert,
        (
            "enable federation and its splunk source, pointed at a splunk-stub that fails every search",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        (
            "disable federation, the splunk source and the integration, zero its error count",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        mode_override="I-2",
    ),
)
