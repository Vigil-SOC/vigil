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
#   --providers          Delete every LLM provider (the hard gate — this alone re-shows the wizard)
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

# No `set -u`: macOS ships bash 3.2, where expanding an empty array (AUTH,
# data_sources) under `-u` is a fatal "unbound variable".
set -eo pipefail

B="${VIGIL_API:-http://localhost:6987/api}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'

AUTH=()
[ -n "${VIGIL_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${VIGIL_TOKEN}")

# --- prerequisites --------------------------------------------------------
command -v curl   >/dev/null 2>&1 || { echo "curl is required"   >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }

get()  { curl -fsS "${AUTH[@]}" "$B$1"; }
del()  { curl -fsS "${AUTH[@]}" -X DELETE "$B$1"; }
put()  { curl -fsS "${AUTH[@]}" -X PUT  -H 'Content-Type: application/json' -d "$2" "$B$1"; }
post() { curl -fsS "${AUTH[@]}" -X POST -H 'Content-Type: application/json' -d "$2" "$B$1"; }

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
    -h|--help)     sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
[ "$do_providers"   = true ] && planned+=("delete all LLM providers (re-fires the hard gate)")
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
  ids=$(get /llm/providers/ | python3 -c "import sys,json;[print(p['provider_id']) for p in json.load(sys.stdin)]")
  if [ -z "$ids" ]; then echo "  providers: already empty"; else
    while read -r id; do [ -n "$id" ] && { del "/llm/providers/$id" >/dev/null; echo -e "  ${GREEN}deleted provider${NC} $id"; }; done <<< "$ids"
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
