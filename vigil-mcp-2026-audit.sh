#!/usr/bin/env bash
#
# mcp-2026-audit.sh — MCP 2026-07-28 ("MCP 2.0") migration readiness audit
#
# Run from the vigil repo root (with submodules checked out):
#   git submodule update --init --recursive
#   ./mcp-2026-audit.sh            # full audit
#   ./mcp-2026-audit.sh --summary  # counts only
#
# Exit code: number of finding categories with hits (0 = clean).

set -uo pipefail

SUMMARY_ONLY=false
[[ "${1:-}" == "--summary" ]] && SUMMARY_ONLY=true

# Directories that contain MCP client or server code.
SCAN_DIRS=()
for d in mcp-servers backend services daemon core tools contrib scripts; do
  [[ -d "$d" ]] && SCAN_DIRS+=("$d")
done

if [[ ${#SCAN_DIRS[@]} -eq 0 ]]; then
  echo "No expected source dirs found. Run from the vigil repo root." >&2
  exit 1
fi

# Prefer ripgrep; fall back to grep -r.
if command -v rg >/dev/null 2>&1; then
  SEARCH() { rg -n --no-heading -S -g '!*.lock' -g '!node_modules' -g '!venv' -g '!*.min.js' "$1" "${SCAN_DIRS[@]}" 2>/dev/null; }
else
  SEARCH() { grep -rnE --exclude-dir={node_modules,venv,.git,dist,build} "$1" "${SCAN_DIRS[@]}" 2>/dev/null; }
fi

FAIL_CATEGORIES=0

section() {
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo "  $1"
  echo "════════════════════════════════════════════════════════════════"
}

check() {
  # check <severity> <label> <pattern> <advice>
  local sev="$1" label="$2" pattern="$3" advice="$4"
  local hits count
  hits="$(SEARCH "$pattern")"
  count=$(printf '%s' "$hits" | grep -c . || true)
  if [[ "$count" -gt 0 ]]; then
    FAIL_CATEGORIES=$((FAIL_CATEGORIES + 1))
    echo
    echo "[$sev] $label — $count hit(s)"
    echo "  ↳ $advice"
    if ! $SUMMARY_ONLY; then
      printf '%s\n' "$hits" | head -25 | sed 's/^/      /'
      [[ "$count" -gt 25 ]] && echo "      ... ($((count - 25)) more)"
    fi
  else
    echo "[ok ] $label"
  fi
}

# ──────────────────────────────────────────────────────────────────
section "1. SDK & dependency versions"
# ──────────────────────────────────────────────────────────────────
echo
echo "Python MCP/Anthropic pins found:"
SEARCH '^\s*(mcp|fastmcp|anthropic)([<>=~!].*)?$' | grep -Ei 'requirements.*\.txt|pyproject\.toml|setup\.cfg' | sed 's/^/    /' || echo "    (none — check manually)"
echo
echo "TypeScript MCP SDK pins found:"
SEARCH '@modelcontextprotocol/(sdk|server)' | grep -E 'package\.json' | sed 's/^/    /' || echo "    (none in tree — npx-launched servers resolve at runtime)"
echo
echo "  ↳ Action: track Python + TS SDK releases during the RC window;"
echo "    Tier 1 SDKs ship 2026-07-28 support before the July 28 final."

# ──────────────────────────────────────────────────────────────────
section "2. Hard breaking changes"
# ──────────────────────────────────────────────────────────────────

check "FIX " "Custom error code -32002 (resource not found)" \
  '\-32002' \
  "SEP-2164: missing-resource now returns standard -32602 Invalid Params. Update matchers; accept both during transition."

check "FIX " "Experimental Tasks API (esp. tasks/list — removed)" \
  'tasks/(list|result|create)|experimental.*tasks|TaskHandle' \
  "Tasks moved to an extension with a new lifecycle (tasks/get|update|cancel, server-directed creation). tasks/list is gone."

check "WARN" "Hardcoded protocolVersion pins" \
  '2024-11-05|2025-03-26|2025-06-18|2025-11-25' \
  "Old date-version literals. Fine if SDK-negotiated; a problem if asserted/validated in custom client code."

check "WARN" "Session-ID / handshake assumptions in custom transport code" \
  'Mcp-Session-Id|mcp_session_id|initialize.*initialized|session_id.*mcp' \
  "SEP-2567/2575: session header and initialize handshake removed. Only matters if you wrote custom HTTP transport/client code (the SDK handles stdio)."

# ──────────────────────────────────────────────────────────────────
section "3. Deprecated core features (12-month removal window)"
# ──────────────────────────────────────────────────────────────────

check "WARN" "MCP-level logging" \
  'notifications/message|send_log_message|set_logging_level|setLevel.*logging/setLevel|logging/setLevel' \
  "Deprecated. Migrate servers to stderr (stdio) or OpenTelemetry. Plain Python 'logging' to stderr is already fine."

check "WARN" "Sampling (server-initiated LLM calls)" \
  'sampling/createMessage|create_message\(|samplingCapab|request_sampling' \
  "Deprecated. Replace with direct LLM provider API calls from the server (you already route via Bifrost/Anthropic)."

check "WARN" "Roots" \
  'roots/list|list_roots|RootsCapab' \
  "Deprecated. Replace with explicit tool parameters, resource URIs, or server config."

# ──────────────────────────────────────────────────────────────────
section "4. Elicitation / approval-flow touchpoints"
# ──────────────────────────────────────────────────────────────────

check "PLAN" "Elicitation usage (approval workflow!)" \
  'elicit|elicitation/create|InputRequired|input_required' \
  "SEP-2322: prompts now flow as InputRequiredResult + requestState re-issue instead of a held SSE stream. Audit approval.py and any human-in-the-loop paths."

check "INFO" "Long-running tool calls (Tasks-extension candidates)" \
  'sandbox.*submit|submit.*(file|sample|url)|poll.*report|wait_for|asyncio\.sleep\([0-9]{2,}' \
  "Sandbox submissions (Joe/ANY.RUN/Hybrid) are natural Tasks candidates. Target the new extension, not the 2025-11-25 experimental API."

# ──────────────────────────────────────────────────────────────────
section "5. Transport & caching posture"
# ──────────────────────────────────────────────────────────────────

check "INFO" "HTTP/SSE transports in use" \
  'streamable.?http|sse_client|SseServerTransport|/mcp.*(POST|route)|mcp.*sse' \
  "Any server exposed over HTTP needs Mcp-Method/Mcp-Name headers (SEP-2243) and the stateless model. stdio servers are unaffected."

check "INFO" "tools/list caching in the client" \
  'tools/list|list_tools|tool_cache|cached_tools' \
  "New ttlMs/cacheScope fields (SEP-2549) give a spec-compliant cache policy — worth adopting with 100+ tools across servers."

# ──────────────────────────────────────────────────────────────────
section "6. mcp-config.json hygiene"
# ──────────────────────────────────────────────────────────────────
if [[ -f mcp-config.json ]]; then
  echo
  echo "Unpinned npx/uvx launchers (will silently pick up new-spec releases):"
  grep -nE '"(npx|uvx)"' -A2 mcp-config.json | grep -E '\-y", "[^"@]+"' | sed 's/^/    /' \
    && echo "  ↳ Consider pinning (pkg@x.y.z) until you've validated each against the new SDK." \
    || echo "    (all pinned or none found)"
else
  echo "  mcp-config.json not found at repo root."
fi

# ──────────────────────────────────────────────────────────────────
section "Summary"
# ──────────────────────────────────────────────────────────────────
echo
echo "  Categories with findings: $FAIL_CATEGORIES"
echo "  Severities: FIX = breaks on 2026-07-28 · WARN = deprecated/fragile"
echo "              PLAN = design work needed · INFO = opportunity"
echo
echo "  Spec/changelog: https://modelcontextprotocol.io/specification/draft/changelog"

exit "$FAIL_CATEGORIES"
