# MCP Tool Support — All LLM Providers

> **Summary:** SentinelOne (and every other MCP integration) now works with Claude, OpenAI, Ollama, and any future provider — not just Anthropic.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Provider Routing](#provider-routing)
- [Agentic Tool-Call Loop](#agentic-tool-call-loop)
- [SentinelOne Integration](#sentinelone-integration)
- [Startup Credential Flow](#startup-credential-flow)
- [Agent Tool Filtering](#agent-tool-filtering)
- [File Changes](#file-changes)
- [Limitations](#limitations)

---

## Overview

Before this work, MCP tools (SentinelOne alerts, threat intel, etc.) were only available when **Anthropic/Claude** was the active provider. OpenAI and Ollama paths used a guardrail prompt that explicitly told the model it had no tools.

After this work, **every provider** gets full MCP tool access through a unified agentic loop.

| Before | After |
|--------|-------|
| Claude → ✅ MCP tools work | Claude → ✅ MCP tools work |
| OpenAI → ❌ No tools, generic response | OpenAI → ✅ MCP tools work |
| Ollama → ❌ No tools, generic response | Ollama → ✅ MCP tools work |
| Container restart → ❌ SentinelOne disconnects | Container restart → ✅ Auto-reconnects |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Vigil Assistant UI                       │
│              "List recent alerts" / Agent chat               │
└────────────────────────────┬────────────────────────────────┘
                             │ POST /api/claude/chat/stream
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                    backend/api/claude.py                      │
│                                                             │
│  resolve provider ──► is Anthropic?                         │
│                              │                              │
│              ┌───────────────┴──────────────┐              │
│              ▼ Yes                           ▼ No           │
│     ClaudeService path              LLMRouter path          │
│    (existing, unchanged)         (NEW: agentic loop)        │
└──────────────┬───────────────────────────────┬─────────────┘
               │                               │
               ▼                               ▼
┌──────────────────────┐         ┌─────────────────────────┐
│   Anthropic SDK       │         │   LLMRouter / Bifrost   │
│   + MCP tools         │         │   + MCP tools (NEW)     │
│   (always worked)     │         │   OpenAI function call  │
└──────────────────────┘         └────────────┬────────────┘
                                              │ tool_calls?
                                              ▼
                                 ┌─────────────────────────┐
                                 │   MCPClient.call_tool()  │
                                 │   sentinelone_list_alerts│
                                 │   → purple-mcp stdio     │
                                 │   → SentinelOne API      │
                                 └────────────┬────────────┘
                                              │ results
                                              ▼
                                 ┌─────────────────────────┐
                                 │  Feed results back to    │
                                 │  LLM → final answer      │
                                 └─────────────────────────┘
```

---

## Provider Routing

```python
# backend/api/claude.py

active_provider = _select_active_provider(provider_id)

use_router = (
    active_provider is not None
    and active_provider.provider_type != "anthropic"
)

# use_router = True  →  OpenAI, Ollama, any future provider
# use_router = False →  Anthropic (ClaudeService path)
```

Both paths now have identical MCP tool capability. The only difference is the transport layer (Anthropic SDK vs OpenAI-compatible Bifrost endpoint).

---

## Agentic Tool-Call Loop

The router path runs up to **6 rounds** of tool calls before returning a final text response:

```
Round 1: Send user message + all MCP tools to LLM
         │
         ├── LLM returns text? ──────────────────► Done, emit response
         │
         └── LLM returns tool_calls?
                    │
                    ▼
             Execute each tool via MCPClient
                    │
                    ▼
             Append tool results to message history
                    │
                    ▼
Round 2: Send updated history + tools to LLM
         │
         ├── LLM returns text? ──────────────────► Done, emit response
         │
         └── LLM returns tool_calls? ──► ... (up to round 6)
```

```python
for _turn in range(6):
    _result = await LLMRouter().dispatch(
        provider=active_provider,
        messages=_loop_msgs,
        system_prompt=_effective_sys,
        tools=_oai_tools,
        ...
    )
    _tcs = _result.get("tool_calls") or []
    if not _tcs:
        response = _result.get("content", "")
        break                            # ← text response, done

    # Execute tool calls
    for tc in _tcs:
        tool_result = await mcp_client.call_tool(server, tool, args)
        _loop_msgs.append({"role": "tool", "content": tool_result})
```

**System prompt switching:**

| Situation | System Prompt |
|-----------|--------------|
| MCP tools available | `ROUTER_WITH_TOOLS_SYSTEM_PROMPT` — "you have live security tools, use them" |
| No MCP tools connected | `ROUTER_NO_TOOLS_SYSTEM_PROMPT` — "you have no tools, don't hallucinate" |
| Agent with own prompt | Agent's system prompt (takes priority) |

---

## SentinelOne Integration

### Tools Available (33 total)

| Category | Tools |
|----------|-------|
| **Alerts** | `sentinelone_list_alerts`, `sentinelone_search_alerts`, `sentinelone_get_alert`, `sentinelone_get_alert_history`, `sentinelone_get_alert_notes`, `sentinelone_get_alert_investigation_report` |
| **Vulnerabilities** | `sentinelone_list_vulnerabilities`, `sentinelone_get_vulnerability`, `sentinelone_get_vulnerability_history`, `sentinelone_get_vulnerability_notes` |
| **Inventory** | `sentinelone_list_inventory_items`, `sentinelone_search_inventory_items`, `sentinelone_get_inventory_item` |
| **Misconfigurations** | `sentinelone_list_misconfigurations`, `sentinelone_search_misconfigurations`, `sentinelone_get_misconfiguration` |
| **Threat Intel** | `sentinelone_threat_intel_by_ip`, `sentinelone_threat_intel_by_hash`, `sentinelone_threat_intel_by_domain`, `sentinelone_threat_intel_by_url`, `sentinelone_threat_intel_get_file_behavior`, `sentinelone_threat_intel_get_file_relationships`, `sentinelone_threat_intel_search` |
| **CVE** | `sentinelone_cve_database_status`, `sentinelone_cve_search_by_id`, `sentinelone_cve_search_by_vendor` |
| **Query / AI** | `sentinelone_powerquery`, `sentinelone_purple_ai` |
| **Utilities** | `sentinelone_get_timestamp_range`, `sentinelone_iso_to_unix_timestamp` |

### MCP Server Config (`mcp-servers/mcp_config.json`)

```json
"sentinelone": {
  "command": "uvx",
  "args": ["--from", "git+https://github.com/Sentinel-One/purple-mcp.git",
           "purple-mcp", "--mode", "stdio"],
  "env": {
    "PURPLEMCP_CONSOLE_TOKEN":   "${SENTINELONE_API_TOKEN}",
    "PURPLEMCP_CONSOLE_BASE_URL": "${SENTINELONE_CONSOLE_URL}"
  }
}
```

Credentials are injected at connection time via `${VAR}` substitution — never hardcoded.

---

## Startup Credential Flow

**Problem before:** `os.environ` is ephemeral. After a container restart, SentinelOne credentials were gone, purple-mcp couldn't authenticate, and the tools cache stayed empty.

**Solution:** `restore_all_integration_secrets()` runs at startup before MCP connections.

```
Container starts
      │
      ▼
startup_event() in backend/main.py
      │
      ├── SecretsManager ready (encrypted ~/.vigil/secrets.enc)
      │
      ├── restore_all_integration_secrets()          ◄── NEW
      │     │  Reads SENTINELONE_API_TOKEN,
      │     │        SENTINELONE_CONSOLE_URL,
      │     │        CROWDSTRIKE_CLIENT_SECRET, ...
      │     └─► os.environ["SENTINELONE_API_TOKEN"] = <value>
      │         os.environ["SENTINELONE_CONSOLE_URL"] = <value>
      │
      └── MCP client initialization
            │
            └── connect_to_server("sentinelone")
                  │
                  ├── substitutes ${SENTINELONE_API_TOKEN} from os.environ ✓
                  │
                  └── spawns purple-mcp subprocess
                        │
                        └── 33 tools → tools_cache → MCPRegistry
```

**Credential storage path (UI → encrypted store → env → subprocess):**

```
UI form (Settings → Integrations → SentinelOne)
  │  api_token, console_url
  ▼
split_secrets() → set_secret("SENTINELONE_API_TOKEN", value)
                             │
                             ├──► ~/.vigil/secrets.enc  (persists across restarts)
                             └──► os.environ             (lost on restart, restored above)
```

---

## Agent Tool Filtering

When a request includes an `agent_id`, the tool set is filtered by the agent's `recommended_tools`.

```
agent.recommended_tools = ["sentinelone_list_alerts", "sentinelone_get_alert"]
         │
         ▼
_filter_openai_tools(all_tools, server_map, allowed_tools)
         │
         ├── Match exact name OR suffix after first underscore
         │
         ├── Matches found? ──► Return filtered tool set
         │
         └── No matches? ──────► Return ALL tools (fallback)
                                  (built-in agents list internal Vigil tool
                                   names like "list_findings", not MCP names)
```

| Agent Type | `recommended_tools` | MCP Tools Passed |
|------------|--------------------|--------------------|
| Built-in (Triage, Investigator, etc.) | `["list_findings", "get_finding"]` | All connected MCP tools (fallback) |
| Custom agent with MCP tools | `["sentinelone_list_alerts"]` | Only `sentinelone_list_alerts` |
| No agent (plain chat) | — | All connected MCP tools |

---

## File Changes

| File | Change |
|------|--------|
| `backend/api/claude.py` | Add agentic tool-call loop to `chat()` and `chat_stream()` for non-Anthropic providers; add `_get_openai_mcp_tools()`, `_mcp_tool_result_text()`, `_filter_openai_tools()` helpers; add `allowed_tools` load to streaming agent block |
| `backend/main.py` | Call `restore_all_integration_secrets()` at startup before MCP init |
| `services/integration_secrets.py` | Add `restore_all_integration_secrets()`; add `console_url` to SentinelOne secret fields |
| `services/mcp_client.py` | Fix `PersistentServerSession` to use background asyncio task (anyio cancel scope fix); register tools in MCPRegistry on every `connect_to_server()` |
| `services/bifrost_admin.py` | Fix `push_provider_key()` to use `/api/providers/{name}/keys` endpoint; add `_get_provider_keys()` helper |
| `docker/docker-compose.yml` | Mount `../.vigil:/tmp/.vigil` so secrets store survives container rebuilds |
| `mcp-servers/mcp_config.json` | Add SentinelOne/purple-mcp server entry |

---

## Limitations

**Streaming with tool calls:** When a non-Anthropic provider calls a tool, the response is not streamed character-by-character. The agentic loop runs non-streaming internally, then emits the complete final answer as a single SSE event. Responses without tool calls stream normally.

**Ollama tool support:** Tool/function calling requires a model that supports it. Compatible models include `llama3.1`, `llama3.2`, `qwen2.5`, `mistral-nemo`, `deepseek-r1`. Models without function calling support (`llama2`, `codellama`) will receive a text-only response and cannot use MCP tools.

**Tool round limit:** The agentic loop caps at 6 rounds to prevent runaway tool chains. Complex multi-step investigations may hit this limit.
