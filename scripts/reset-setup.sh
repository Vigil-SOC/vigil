#!/bin/bash
# Reset the first-access setup state so the onboarding wizard (/setup) starts fresh.
#
# Each onboarding step derives "ready" live from backend state (see
# clients/web/src/screens/setup/setupSteps.ts) — there is no persisted "done"
# flag — so clearing the underlying state is all it takes to redo the wizard.
#
# Two stores back the "connect an AI provider" step, and EITHER satisfies it:
# the legacy llm_provider_configs table and Bifrost's own key store (#761). A
# reset has to drain both or the step stays green — clearing Postgres alone
# leaves a verified gateway key holding the gate open.
#
# Usage:
#   ./scripts/reset-setup.sh                 # providers + bifrost keys + assignments + budget + autonomy
#   ./scripts/reset-setup.sh --status        # show current state, change nothing
#   ./scripts/reset-setup.sh --providers     # only clear the legacy LLM providers
#   ./scripts/reset-setup.sh --bifrost       # only disable the gateway's keys
#   ./scripts/reset-setup.sh --data-source splunk   # also disconnect a real data-source server
#   ./scripts/reset-setup.sh --all -y        # everything, no confirmation prompt
#
# Options:
#   --providers          Clear every LLM provider. Deletes each one; for the last active
#                        default of a type — which the API can neither delete nor unset
#                        (the single-default guard 409s) — it clears the default flag
#                        directly in Postgres, then deletes. Falls back to deactivating
#                        that row if the DB isn't reachable. On its own this no longer
#                        re-fires the gate: pair it with --bifrost (or use --all).
#   --bifrost            Disable every enabled key in Bifrost's store. Disabled, not
#                        deleted — the credential and its ~/.vigil/secrets.enc ref
#                        survive, so re-enabling is one click in Settings → AI Config
#                        and nothing has to be retyped. Disabling through the proxy
#                        retires the key's mirror row with it; the env-backed
#                        fallback below writes straight to the gateway and does not,
#                        so pair this with --providers (or use --all) to be sure the
#                        gate is drained.
#   --assignments        Clear all per-agent model assignments
#   --budget             Clear the Bifrost virtual key + spend cap
#   --autonomy           Disable the autonomous orchestrator (preserves its cost caps)
#   --data-source NAME   Disconnect MCP server NAME (repeatable). Use ONLY for real telemetry
#                        sources (splunk, elastic, ...) — never Vigil's internal servers.
#   --all                providers + bifrost + assignments + budget + autonomy (NOT data sources)
#   --status             Print current setup state and exit
#   -y, --yes            Skip the confirmation prompt
#   -h, --help           Show this help
#
# Env:
#   VIGIL_API     Backend API base URL (default: http://localhost:6987/api)
#   BIFROST_URL   Gateway admin API root, used only by the --bifrost fallback
#                 below (default: http://localhost:8080)
#
# Auth: assumes DEV_MODE=true (auth bypassed). Set VIGIL_TOKEN to send a
# Bearer token if you run against an authenticated backend.
#
# DB step: --providers may need to clear a stale default flag directly in
# Postgres (the API can't unset or delete the last default of a type). This
# reuses the app's own DB connection, so it runs best from the repo root with
# the project venv (SQLAlchemy + the same POSTGRES_* / .env config the backend
# uses). If the DB isn't reachable it falls back to deactivating that provider.
#
# Gateway step: --bifrost writes through the console's own /api/bifrost proxy so
# credentials keep flowing through the secrets store. An env-backed key has no
# stored secret for the proxy to substitute, so that one write goes straight to
# BIFROST_URL re-declaring the key's env reference — which moves no secret.

# No `set -u`: macOS ships bash 3.2, where expanding an empty array (AUTH,
# data_sources) under `-u` is a fatal "unbound variable".
set -eo pipefail

B="${VIGIL_API:-http://localhost:6987/api}"
# Gateway admin root. Only the --bifrost env-backed fallback talks to it
# directly; every other gateway call goes through the console proxy at $B.
BF_ADMIN="${BIFROST_URL:-http://localhost:8080}/api"

# Repo root (this script lives in scripts/) and a Python that has the project's
# deps — used only by the DB fallback in the --providers reset. Prefer the venv
# so the step works without `source venv/bin/activate`; degrade to python3.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="python3"
[ -x "$REPO_ROOT/venv/bin/python" ] && PYTHON="$REPO_ROOT/venv/bin/python"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'

AUTH=()
[ -n "${VIGIL_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${VIGIL_TOKEN}")

# --- prerequisites --------------------------------------------------------
command -v curl   >/dev/null 2>&1 || { echo "curl is required"   >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

get()  { curl -fsS "${AUTH[@]}" "$B$1"; }
del()  { curl -fsS "${AUTH[@]}" -X DELETE "$B$1"; }
# DELETE that prints the HTTP status instead of aborting on 4xx. The provider
# delete endpoint legitimately 409s on the last active default of a type (the
# single-default guard added in #336) — we detect that and deactivate instead.
del_code() { curl -s -o /dev/null -w '%{http_code}' "${AUTH[@]}" -X DELETE "$B$1"; }
put()  { curl -fsS "${AUTH[@]}" -X PUT  -H 'Content-Type: application/json' -d "$2" "$B$1"; }
post() { curl -fsS "${AUTH[@]}" -X POST -H 'Content-Type: application/json' -d "$2" "$B$1"; }

# Clear a provider's is_default flag straight in Postgres. The API deliberately
# can't: the single-default guard (#336) 409s on unsetting the only default of a
# type, and the delete guard 409s on deleting it — so a reset can never drain the
# last default via HTTP alone. Best-effort: reuses the app's own DB connection
# (same POSTGRES_* / .env the backend reads) and returns non-zero, changing
# nothing, if the DB or deps aren't reachable from this shell.
clear_provider_default() {
  RESET_PROVIDER_ID="$1" REPO_ROOT="$REPO_ROOT" "$PYTHON" - <<'PY'
import os, sys

root = os.environ["REPO_ROOT"]
sys.path.insert(0, root)
try:
    from dotenv import load_dotenv  # mirror start.sh's `set -a; source .env`

    load_dotenv(os.path.join(root, ".env"))  # no-op if absent; never overrides
except Exception:
    pass
try:
    from sqlalchemy import text

    from core.storage.connection import get_db_manager

    m = get_db_manager()
    if m._engine is None:
        m.initialize()
    with m.session_scope() as s:
        s.execute(
            text(
                "UPDATE llm_provider_configs SET is_default = FALSE "
                "WHERE provider_id = :pid"
            ),
            {"pid": os.environ["RESET_PROVIDER_ID"]},
        )
except Exception as exc:  # DB unreachable / deps missing → caller falls back
    print(f"db-clear failed: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

# Shared by --status and --bifrost: everything that has to know how a Bifrost
# key is shaped. Kept in one place because the read side (which keys hold the
# gate open) and the write side (how to disable one without corrupting its
# credential) have to agree.
bifrost_py() {
  VIGIL_API="$B" BF_ADMIN="$BF_ADMIN" AUTH_TOKEN="${VIGIL_TOKEN:-}" BF_MODE="$1" "$PYTHON" - <<'PY'
import json, os, urllib.error, urllib.request

API = os.environ["VIGIL_API"].rstrip("/")
BF = os.environ["BF_ADMIN"].rstrip("/")
TOKEN = os.environ.get("AUTH_TOKEN", "")
MODE = os.environ["BF_MODE"]

GREEN = "\033[0;32m"; YELLOW = "\033[1;33m"; DIM = "\033[2m"; NC = "\033[0m"


def req(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    if TOKEN:
        r.add_header("Authorization", "Bearer " + TOKEN)
    with urllib.request.urlopen(r, timeout=15) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def env_ref(field):
    """Re-declare a credential that lives in the environment, or None.

    Returns the same {value, env_var, from_env} shape Bifrost hands back, with
    the masked value dropped — echoing a mask back would store the mask as the
    credential and every call would then 401.
    """
    if isinstance(field, dict) and field.get("from_env") and field.get("env_var"):
        return {"value": "", "env_var": field["env_var"], "from_env": True}
    return None


_verdicts = None


def verdicts():
    """The backend's routability map, fetched once per run.

    Raises rather than degrading to an empty map: every key would then read as
    non-routable and the reset would report a clean sweep it never made.
    """
    global _verdicts
    if _verdicts is None:
        got = req(f"{API}/bifrost/routability")
        if not isinstance(got, dict) or "keys" not in got:
            raise RuntimeError(
                "routability check unavailable — is the backend running?"
            )
        _verdicts = got
    return _verdicts


def routable(k):
    """Does this key hold the provider step green? Backend's verdict."""
    return bool((verdicts().get("keys") or {}).get(k.get("id"), {}).get("routable"))


# Bifrost answers an empty collection with null rather than [], so `or []`
# rather than a dict default -- a provider whose last key was deleted was
# otherwise reported as "unreachable (NoneType is not iterable)".
def providers():
    return (req(f"{API}/bifrost/providers") or {}).get("providers") or []


def keys_of(name):
    return (req(f"{API}/bifrost/providers/{name}/keys") or {}).get("keys") or []


def disable(provider, key):
    """Flip one key to disabled, carrying everything else through untouched.

    Bifrost has no enabled-only update, so the whole key is rewritten and every
    field we don't want to lose has to be restated — a PUT that omits `name`
    blanks it.
    """
    body = {
        "name": key.get("name") or "",
        "models": key.get("models") or ["*"],
        "blacklisted_models": key.get("blacklisted_models") or [],
        "weight": key.get("weight", 1),
        "use_for_batch_api": bool(key.get("use_for_batch_api")),
        "enabled": False,
    }
    # Ollama's credential is a URL the operator typed, not a secret the proxy
    # masks or stores, so the block has to come along or Bifrost loses the
    # endpoint. Vertex's project/region travel the same way; its
    # service-account JSON is left out so the proxy substitutes the stored one.
    ollama = key.get("ollama_key_config")
    if isinstance(ollama, dict):
        body["ollama_key_config"] = {"url": env_ref(ollama.get("url")) or ollama.get("url")}
    vertex = key.get("vertex_key_config")
    if isinstance(vertex, dict):
        body["vertex_key_config"] = {
            f: v for f, v in vertex.items() if f != "auth_credentials"
        }

    # The proxy first: it substitutes the plaintext it holds for any key a human
    # set. A 400 means it holds none — an env-backed key — so re-declare that
    # reference straight to the gateway instead.
    try:
        req(f"{API}/bifrost/providers/{provider}/keys/{key['id']}", "PUT", body)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
    ref = env_ref(key.get("value"))
    if ref is None:
        return False
    body["value"] = ref
    req(f"{BF}/providers/{provider}/keys/{key['id']}", "PUT", body)
    return True


if MODE == "status":
    try:
        live = [
            f"{p['name']}/{k.get('name') or k['id'][:8]}"
            for p in providers()
            for k in keys_of(p["name"])
            if routable(k)
        ]
    except Exception as exc:  # noqa: BLE001 — advisory line, never fatal
        print(f"unreachable ({exc})")
    else:
        print(", ".join(live) + " (holds the gate open)" if live else "none routable")
    raise SystemExit(0)

# MODE == "reset"
try:
    provs = providers()
except Exception as exc:  # noqa: BLE001
    print(f"  {YELLOW}bifrost unreachable{NC} {DIM}({exc}) — no keys disabled{NC}")
    raise SystemExit(0)

# Before the loop, so the refusal above is a message, not a partial sweep.
try:
    verdicts()
except Exception as exc:  # noqa: BLE001
    print(f"  {YELLOW}cannot tell which keys route{NC} {DIM}({exc}){NC}")
    print(f"  {DIM}nothing disabled — a reset that reports a clean sweep it did "
          f"not make is worse than one that stops{NC}")
    raise SystemExit(1)

changed = stuck = inert = 0
for p in provs:
    name = p["name"]
    try:
        keys = keys_of(name)
    except Exception as exc:  # noqa: BLE001
        print(f"  {YELLOW}could not list keys{NC} for {name} {DIM}({exc}){NC}")
        continue
    for k in keys:
        if not k.get("enabled"):
            continue
        # An env-placeholder key whose variable is unset cannot be rewritten.
        if not routable(k):
            inert += 1
            continue
        label = f"{name}/{k.get('name') or k['id'][:8]}"
        try:
            done = disable(name, k)
        except Exception as exc:  # noqa: BLE001
            print(f"  {YELLOW}could not disable{NC} {label} {DIM}({exc}){NC}")
            stuck += 1
            continue
        if done:
            print(f"  {GREEN}disabled bifrost key{NC} {label}")
            changed += 1
        else:
            print(
                f"  {YELLOW}left enabled{NC} {label} {DIM}(no stored credential "
                f"and no env reference — disable it in Settings → AI Config){NC}"
            )
            stuck += 1

if not changed and not stuck:
    print("  bifrost keys: none were routable")
if inert:
    print(
        f"  {DIM}({inert} enabled but non-routable key(s) left alone — they don't "
        f"hold the gate){NC}"
    )
if stuck:
    print(
        f"  {YELLOW}the provider step will stay green{NC} {DIM}until the {stuck} "
        f"key(s) above are disabled{NC}"
    )
PY
}

# --- argument parsing -----------------------------------------------------
do_providers=false; do_assignments=false; do_budget=false; do_autonomy=false
do_bifrost=false; bifrost_failed=false
status_only=false; assume_yes=false
data_sources=()

if [ $# -eq 0 ]; then
  do_providers=true; do_bifrost=true; do_assignments=true; do_budget=true; do_autonomy=true
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --providers)   do_providers=true ;;
    --bifrost)     do_bifrost=true ;;
    --assignments) do_assignments=true ;;
    --budget)      do_budget=true ;;
    --autonomy)    do_autonomy=true ;;
    --data-source) shift; [ $# -gt 0 ] || { echo "--data-source needs a server name" >&2; exit 1; }; data_sources+=("$1") ;;
    --all)         do_providers=true; do_bifrost=true; do_assignments=true; do_budget=true; do_autonomy=true ;;
    --status)      status_only=true ;;
    -y|--yes)      assume_yes=true ;;
    -h|--help)     awk 'NR>1 && /^# DB step:/{exit} NR>1 && /^#/{sub(/^# ?/,"");print}' "$0"; exit 0 ;;
    *)             echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

# --- show current state ---------------------------------------------------
show_status() {
  echo -e "${DIM}API: $B${NC}"
  echo -n "  LLM providers     : "
  # An inactive row is marked as such: only an ACTIVE default satisfies the
  # provider step, and a retired Bifrost mirror row (see core/llm/bifrost/
  # mirror.py) stays behind after a reset, where it otherwise reads as a
  # holdout that still needs clearing.
  get /llm/providers/ | python3 -c "import sys,json;d=json.load(sys.stdin);print(', '.join(p['provider_id'] + ('(default)' if p.get('is_default') else '') + ('' if p.get('is_active') else ' [inactive]') for p in d) or 'none')"
  echo -n "  Bifrost keys      : "
  bifrost_py status
  echo -n "  Model assignments : "
  get /ai/config | python3 -c "import sys,json;a=json.load(sys.stdin).get('assignments',{});print(', '.join(a) or 'none')"
  echo -n "  Cost guardrails   : "
  get /analytics/budget | python3 -c "import sys,json;d=json.load(sys.stdin);vk=(d.get('default_vk') or '').strip();print(f'vk set ({vk})' if vk else 'none')"
  echo -n "  Autonomy          : "
  get /config/orchestrator | python3 -c "import sys,json;print('enabled' if json.load(sys.stdin).get('enabled') else 'disabled')"
  echo -n "  Connected MCP     : "
  get /mcp/connections/status | python3 -c "import sys,json;c=[x['name'] for x in json.load(sys.stdin).get('connections',[]) if x.get('connected')];print(', '.join(c) or 'none')"
}

echo -e "${YELLOW}Current setup state${NC}"
show_status
echo

if [ "$status_only" = true ]; then exit 0; fi

# --- confirm --------------------------------------------------------------
planned=()
[ "$do_providers"   = true ] && planned+=("delete all LLM providers (clears the last default's flag in Postgres so it can be removed; re-fires the hard gate)")
[ "$do_bifrost"     = true ] && planned+=("disable every enabled Bifrost key (reversible; credentials kept)")
[ "$do_assignments" = true ] && planned+=("clear all model assignments")
[ "$do_budget"      = true ] && planned+=("clear the budget / virtual key")
[ "$do_autonomy"    = true ] && planned+=("disable the orchestrator")
for s in "${data_sources[@]:-}"; do [ -n "$s" ] && planned+=("disconnect MCP server '$s'"); done

if [ ${#planned[@]} -eq 0 ]; then echo "Nothing selected. See --help."; exit 0; fi

echo -e "${YELLOW}Will:${NC}"
for p in "${planned[@]}"; do echo "  - $p"; done
if [ "$assume_yes" != true ]; then
  read -r -p "Continue? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi
echo

# --- reset actions --------------------------------------------------------
# Clear assignments BEFORE deleting providers: ai_model_configs.provider_id is a
# FK to llm_provider_configs with ON DELETE RESTRICT (infra/database/init/10_ai_model_configs.sql),
# so deleting a provider an assignment still points at 500s. Order matters here.
if [ "$do_assignments" = true ]; then
  comps=$(get /ai/config | python3 -c "import sys,json;[print(k) for k in json.load(sys.stdin).get('assignments',{})]")
  if [ -z "$comps" ]; then echo "  assignments: already empty"; else
    while read -r c; do [ -n "$c" ] && { del "/ai/config/$c" >/dev/null; echo -e "  ${GREEN}cleared assignment${NC} $c"; }; done <<< "$comps"
  fi
fi

if [ "$do_providers" = true ]; then
  # Delete non-defaults first (sort by is_default asc) so each type drains down
  # to its single default last. The backend refuses (409) to delete the only
  # active default of a type — that's the single-default guard (#336) — and also
  # refuses to unset it. For that last row we clear is_default straight in
  # Postgres, then re-issue the delete (which now passes the guard and still runs
  # the FK cascade + Bifrost key reconcile). If the DB can't be reached we fall
  # back to deactivating it: the wizard gate is `is_active && is_default`
  # (clients/web/src/setup/setupSteps.ts), so an inactive provider re-fires it too.
  ids=$(get /llm/providers/ | python3 -c "import sys,json;rows=json.load(sys.stdin);[print(p['provider_id']) for p in sorted(rows,key=lambda p:bool(p.get('is_default')))]")
  if [ -z "$ids" ]; then echo "  providers: already empty"; else
    while read -r id; do
      [ -n "$id" ] || continue
      code=$(del_code "/llm/providers/$id")
      case "$code" in
        200|204) echo -e "  ${GREEN}deleted provider${NC} $id" ;;
        409)     if clear_provider_default "$id"; then
                   code2=$(del_code "/llm/providers/$id")
                   case "$code2" in
                     200|204) echo -e "  ${GREEN}deleted provider${NC} $id ${DIM}(cleared stale default flag first)${NC}" ;;
                     *)       put "/llm/providers/$id" '{"is_active":false}' >/dev/null
                              echo -e "  ${YELLOW}deactivated provider${NC} $id ${DIM}(cleared stale default; delete returned HTTP $code2)${NC}" ;;
                   esac
                 else
                   put "/llm/providers/$id" '{"is_active":false}' >/dev/null
                   echo -e "  ${YELLOW}deactivated provider${NC} $id ${DIM}(only active default of its type; DB unreachable to clear stale default — gate still re-fires)${NC}"
                 fi ;;
        *)       echo -e "  ${RED}unexpected HTTP $code deleting $id${NC}" >&2; exit 1 ;;
      esac
    done <<< "$ids"
  fi
fi

# After the provider deletes, not before: deleting a provider reconciles its
# Bifrost key, so draining Postgres first means fewer keys left to disable here.
if [ "$do_bifrost" = true ]; then
  # Only this step: the reset's later steps are independent of the gateway, and
  # under `set -e` a raise here used to take the budget, the orchestrator and
  # the data sources down with it.
  bifrost_py reset || bifrost_failed=true
fi

if [ "$do_budget" = true ]; then
  put /analytics/budget '{"default_vk":"","budget_limit_usd":0,"enforcement_mode":"warning"}' >/dev/null
  echo -e "  ${GREEN}cleared budget${NC}"
fi

if [ "$do_autonomy" = true ]; then
  # POST takes the *full* config, so round-trip it and flip enabled off to keep the caps.
  body=$(get /config/orchestrator | python3 -c "import sys,json;d=json.load(sys.stdin);d['enabled']=False;print(json.dumps(d))")
  post /config/orchestrator "$body" >/dev/null
  echo -e "  ${GREEN}disabled orchestrator${NC}"
fi

for s in "${data_sources[@]:-}"; do
  [ -z "$s" ] && continue
  put "/mcp/servers/$s/enabled" '{"enabled":false}' >/dev/null
  echo -e "  ${GREEN}disconnected${NC} $s"
done

echo
if [ "${bifrost_failed:-false}" = true ]; then
  echo -e "${YELLOW}Done, except the gateway.${NC} Its keys were left alone — reload"
  echo -e "/setup once you have disabled them, or the provider step stays green."
  exit 1
fi
echo -e "${GREEN}Done.${NC} Reload /setup to redo the wizard."
