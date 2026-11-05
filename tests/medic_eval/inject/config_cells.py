"""The configuration and environment fault cells (SW I2).

Each fault is made the way an operator would make it, on the stack in
compose.eval.yml, and verified by reading Vigil, never Medic. The verify
functions are shared with the Helm cells (helm_cells.py), which run them on a
HelmStack that offers the same methods.

`detect_within_s` is the rule's `for:` where a merged rule has one (I-1.b). The
I-4, I-5, P-1 and L-1 rules are still drafts (#2732-#2736); those cells use 300
until they merge.
"""

from __future__ import annotations

import json
import time

from tests.medic_eval.inject.contract import Cell
from tests.medic_eval.inject.stack import POLL_S, Stack

ELASTIC = "elastic-siem"
ELASTIC_STUB = "http://siem-stub:9200"
GAP = "Elastic configuration incomplete (missing: elasticsearch_url)"
EMBEDDING = "nomic-embed-text"
CHAT = "llama3.1"
WRONG_TOKEN = "not-the-webhook-token"


def _expect(got, want, what: str) -> None:
    assert got == want, f"{what}: got {got!r}, want {want!r}"


def _eventually(s: Stack, what, want, label: str) -> None:
    try:
        s.wait(what, lambda got: got == want, timeout=90)
    except AssertionError as e:
        raise AssertionError(f"{label}: wanted {want!r}; {e}") from None


# ---- I-5.a: webhook token unset ---------------------------------------------
# The daemon reads DAEMON_WEBHOOK_TOKEN once, at start (compose passes it from
# the environment), and fails closed with 503 when it is empty.


def _webhook_token(s: Stack, token: str) -> None:
    s.env["MEDIC_EVAL_WEBHOOK_TOKEN"] = token
    s.up("soc-daemon")  # recreated only if the value changed
    s.wait_daemon()


def i5a_inject(s: Stack) -> None:
    _webhook_token(s, "")


def i5a_revert(s: Stack) -> None:
    _webhook_token(s, s.good_webhook_token)


def i5a_verify(s: Stack, faulted: bool) -> None:
    # The right bearer, either way: only the daemon's own token is gone.
    _eventually(
        s, lambda: s.push(s.good_webhook_token), 503 if faulted else 200, "POST :8081"
    )


# ---- I-5.b: wrong bearer ----------------------------------------------------
# The fault is in the pusher, not the daemon: no restart on either side.


def i5b_inject(s: Stack) -> None:
    s.pusher_token = WRONG_TOKEN


def i5b_revert(s: Stack) -> None:
    s.pusher_token = s.good_webhook_token


def i5b_verify(s: Stack, faulted: bool) -> None:
    _expect(s.push(), 401 if faulted else 200, "POST :8081 as the pusher")


# ---- I-1.b: Elastic enabled, no URL -----------------------------------------
# The daemon builds its Elastic poller at start, so the integration is enabled
# and the daemon restarted, as an operator has to.


def _elastic_state(s: Stack) -> str:
    return s.sql(
        "SELECT enabled || '|' || coalesce(config->>'elasticsearch_url', '') "
        f"FROM integration_configs WHERE integration_id = '{ELASTIC}'"
    )


def i1b_inject(s: Stack) -> None:
    if _elastic_state(s) == "true|":
        return
    s.set_integration(ELASTIC, {}, True)
    s.recreate("soc-daemon")  # a fresh container, so its log starts empty
    s.wait_daemon()


def i1b_revert(s: Stack) -> None:
    if _elastic_state(s) == "false|":
        return
    s.set_integration(ELASTIC, {}, False)
    s.recreate("soc-daemon")
    s.wait_daemon()


def i1b_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        s.wait(lambda: s.logs("soc-daemon"), lambda out: GAP in out, timeout=90)
    else:
        # wait_daemon has seen the poller start; one poll interval for a gap to show.
        time.sleep(POLL_S + 5)
        assert GAP not in s.logs("soc-daemon"), "the gap is still logged"


# ---- I-4.b: integration enabled after the daemon started --------------------
# The daemon builds its poll loops once, at start. Enabling the integration
# then changes the database and nothing else: no poll until the next restart,
# which is the revert. The stub counts the polls that reach it.


def _polls(s: Stack) -> int:
    """Searches the stub has answered since it started."""
    seen = s.py(
        "siem-stub",
        "import urllib.request; print(urllib.request.urlopen('http://localhost:9200/__seen').read().decode())",
    )
    return sum(1 for r in json.loads(seen)["seen"] if "_search" in r["path"])


def i4b_inject(s: Stack) -> None:
    if "I-4.b" in s.active:
        return
    if _elastic_state(s).startswith("true"):
        # An earlier cycle left a daemon that already polls: start one that does not.
        s.set_integration(ELASTIC, {}, False)
        s.recreate("soc-daemon")
        s.wait_daemon()
    s.marks["I-4.b"] = _polls(s)
    s.set_integration(ELASTIC, {"elasticsearch_url": ELASTIC_STUB}, True)
    s.active.add("I-4.b")


def i4b_revert(s: Stack) -> None:
    if "I-4.b" not in s.active:
        return
    s.recreate("soc-daemon")
    s.wait_daemon()
    s.active.discard("I-4.b")


def i4b_verify(s: Stack, faulted: bool) -> None:
    if faulted:
        _expect(_elastic_state(s), f"true|{ELASTIC_STUB}", "integration row")
        _expect(s.push(s.good_webhook_token), 200, "the daemon is up")
        time.sleep(2 * POLL_S + 5)  # two intervals a loop would have used
        _expect(_polls(s), s.marks["I-4.b"], "searches that reached the stub")
        assert "Elastic Security polling loop started" not in s.logs("soc-daemon")
    else:
        s.wait(
            lambda: _polls(s), lambda n: n > s.marks["I-4.b"], timeout=2 * POLL_S + 60
        )


# ---- P-1.c: AGENT_INTERNAL_TOKEN cleared on the agent services --------------
# Blank on both sides of an /internal call is refused; blank on one side is a
# 401 (the backend still holds the real token). Read through the services' own
# auth: agent-serve's answer to the real token, and the backend's answer to the
# token agent-worker holds.


def _agent_token(s: Stack, token: str) -> None:
    s.env["MEDIC_EVAL_AGENT_TOKEN"] = token
    s.up("agent-serve", "agent-worker")
    for svc in ("agent-serve", "agent-worker"):
        s.wait(
            lambda svc=svc: s.compose("ps", "--format", "json", svc).stdout,
            lambda o: '"healthy"' in o,
        )


def p1c_inject(s: Stack) -> None:
    _agent_token(s, "")


def p1c_revert(s: Stack) -> None:
    _agent_token(s, s.agent_token)


def p1c_verify(s: Stack, faulted: bool) -> None:
    # An unknown route: 404 once the token is accepted, 401 before anything is routed.
    _expect(
        s.node("agent-serve", "http://localhost:6989/nope", s.agent_token),
        401 if faulted else 404,
        "agent-serve",
    )
    _expect(
        s.node(
            "agent-worker",
            f"http://{s.backend_host}:6987/internal/pricing/rates?model_id=x&provider_type=ollama",
        ),
        401 if faulted else 200,
        "backend /internal from agent-worker",
    )


# ---- L-1.c: embedding model assigned as the chat model ----------------------

PROVIDER = {
    "provider_id": "eval-ollama",
    "provider_type": "ollama",
    "name": "eval-ollama",
    "base_url": "http://ollama.invalid:11434",
    "default_model": CHAT,
}


def _assign(s: Stack, model: str) -> None:
    status, body = s.api(
        "PUT",
        "/api/ai/config/chat_default",
        {"provider_id": PROVIDER["provider_id"], "model_id": model},
    )
    assert status == 200, (status, body)


def l1c_inject(s: Stack) -> None:
    if s.api("GET", "/api/llm/providers")[0] != 200 or not any(
        p["provider_id"] == PROVIDER["provider_id"]
        for p in s.api("GET", "/api/llm/providers")[1]
    ):
        status, body = s.api("POST", "/api/llm/providers", PROVIDER)
        assert status == 201, (status, body)
    _assign(s, EMBEDDING)


def l1c_revert(s: Stack) -> None:
    _assign(s, CHAT)


def l1c_verify(s: Stack, faulted: bool) -> None:
    model = EMBEDDING if faulted else CHAT
    status, body = s.api("GET", "/api/ai/config")
    _expect(status, 200, "GET /api/ai/config")
    _expect(body["assignments"]["chat_default"]["model_id"], model, "chat_default")
    # The backend's own classification of what is assigned.
    _expect(
        s.py(
            "backend",
            "import sys; from core.llm.providers.registry import is_chat_model; print(is_chat_model(sys.argv[1], sys.argv[2]))",
            "ollama",
            model,
        ),
        str(not faulted),
        "is_chat_model",
    )


CELLS = (
    Cell(
        "I-1.b",
        2,
        120,
        i1b_inject,
        i1b_verify,
        i1b_revert,
        (
            "enable elastic-siem with no elasticsearch_url",
            "docker compose up -d --force-recreate soc-daemon",
        ),
        ("disable elastic-siem", "docker compose up -d --force-recreate soc-daemon"),
    ),
    Cell(
        "I-4.b",
        1,
        300,
        i4b_inject,
        i4b_verify,
        i4b_revert,
        ("enable elastic-siem with a reachable URL while soc-daemon runs",),
        ("docker compose up -d --force-recreate soc-daemon",),
    ),
    Cell(
        "I-5.a",
        2,
        300,
        i5a_inject,
        i5a_verify,
        i5a_revert,
        ("clear DAEMON_WEBHOOK_TOKEN", "docker compose up -d soc-daemon"),
        ("restore DAEMON_WEBHOOK_TOKEN", "docker compose up -d soc-daemon"),
    ),
    Cell(
        "I-5.b",
        2,
        300,
        i5b_inject,
        i5b_verify,
        i5b_revert,
        ("POST /ingest with a wrong bearer token",),
        ("POST /ingest with the right bearer token",),
    ),
    Cell(
        "P-1.c",
        2,
        300,
        p1c_inject,
        p1c_verify,
        p1c_revert,
        ("clear AGENT_INTERNAL_TOKEN", "docker compose up -d agent-serve agent-worker"),
        (
            "restore AGENT_INTERNAL_TOKEN",
            "docker compose up -d agent-serve agent-worker",
        ),
    ),
    Cell(
        "L-1.c",
        2,
        300,
        l1c_inject,
        l1c_verify,
        l1c_revert,
        ("PUT /api/ai/config/chat_default to an embedding model",),
        ("PUT /api/ai/config/chat_default to a chat model",),
    ),
)
