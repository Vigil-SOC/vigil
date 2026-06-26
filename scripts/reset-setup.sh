#!/bin/bash
# Reset the first-access setup state so the onboarding wizard (/setup) starts fresh.
#
# Each onboarding step derives "ready" live from backend state (see
# frontend/src/setup/setupSteps.ts) — there is no persisted "done" flag — so
# clearing the underlying state is all it takes to redo the wizard.
#
# Usage:
#   ./scripts/reset-setup.sh                 # reset providers + assignments + budget + autonomy
#   ./scripts/reset-setup.sh --status        # show current state, change nothing
#   ./scripts/reset-setup.sh --providers     # only re-trigger the hard gate (delete LLM providers)
#   ./scripts/reset-setup.sh --data-source splunk   # also disconnect a real data-source server
#   ./scripts/reset-setup.sh --all -y        # everything, no confirmation prompt
#
# Options:
#   --providers          Clear every LLM provider. Deletes each one; for the last active
#                        default of a type — which the API can neither delete nor unset
#                        (the single-default guard 409s) — it clears the default flag
#                        directly in Postgres, then deletes. Falls back to deactivating
#                        that row if the DB isn't reachable. Either way the wizard re-shows.
#   --assignments        Clear all per-agent model assignments
#   --budget             Clear the Bifrost virtual key + spend cap
#   --autonomy           Disable the autonomous orchestrator (preserves its cost caps)
#   --data-source NAME   Disconnect MCP server NAME (repeatable). Use ONLY for real telemetry
#                        sources (splunk, elastic, ...) — never Vigil's internal servers.
#   --all                providers + assignments + budget + autonomy (NOT data sources)
#   --status             Print current setup state and exit
#   -y, --yes            Skip the confirmation prompt
#   -h, --help           Show this help
#
# Env:
#   VIGIL_API   Backend API base URL (default: http://localhost:6987/api)
#
# Auth: assumes DEV_MODE=true (auth bypassed). Set VIGIL_TOKEN to send a
# Bearer token if you run against an authenticated backend.
#
# DB step: --providers may need to clear a stale default flag directly in
# Postgres (the API can't unset or delete the last default of a type). This
# reuses the app's own DB connection, so it runs best from the repo root with
# the project venv (SQLAlchemy + the same POSTGRES_* / .env config the backend
# uses). If the DB isn't reachable it falls back to deactivating that provider.

# No `set -u`: macOS ships bash 3.2, where expanding an empty array (AUTH,
# data_sources) under `-u` is a fatal "unbound variable".
set -eo pipefail

B="${VIGIL_API:-http://localhost:6987/api}"

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

    from database.connection import get_db_manager

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

# --- argument parsing -----------------------------------------------------
do_providers=false; do_assignments=false; do_budget=false; do_autonomy=false
status_only=false; assume_yes=false
data_sources=()

if [ $# -eq 0 ]; then
  do_providers=true; do_assignments=true; do_budget=true; do_autonomy=true
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --providers)   do_providers=true ;;
    --assignments) do_assignments=true ;;
    --budget)      do_budget=true ;;
    --autonomy)    do_autonomy=true ;;
    --data-source) shift; [ $# -gt 0 ] || { echo "--data-source needs a server name" >&2; exit 1; }; data_sources+=("$1") ;;
    --all)         do_providers=true; do_assignments=true; do_budget=true; do_autonomy=true ;;
    --status)      status_only=true ;;
    -y|--yes)      assume_yes=true ;;
    -h|--help)     sed -n '2,41p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)             echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

# --- show current state ---------------------------------------------------
show_status() {
  echo -e "${DIM}API: $B${NC}"
  echo -n "  LLM providers     : "
  get /llm/providers/ | python3 -c "import sys,json;d=json.load(sys.stdin);print(', '.join(f\"{p['provider_id']}(default)\" if p.get('is_default') else p['provider_id'] for p in d) or 'none')"
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
# FK to llm_provider_configs with ON DELETE RESTRICT (database/init/10_ai_model_configs.sql),
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
  # (frontend/src/setup/setupSteps.ts), so an inactive provider re-fires it too.
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
echo -e "${GREEN}Done.${NC} Reload /setup to redo the wizard."
