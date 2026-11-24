# vigil-defender

Defender workflows for a **self-hosted [Vigil SOC](https://vigilsoc.org)** instance, inside Claude Code: triage, enrichment, detection drafting and incident-response briefs, with guardrails against prompt injection and unintended side effects.

> **Unofficial.** Not made, endorsed or supported by the Vigil project or DeepTempo. Vigil's Terms (s.4) forbid presenting software as an official product; this plugin is a community add-on. Vigil's own first-party plugin is `vigil-soc` (see below).

## What research found (and what it means for this design)

Verified against vigilsoc.org and the Vigil repo on 2026-11-18:

- **vigilsoc.org is a project website, not a service.** Vigil is open source (Apache-2.0), runs on *your* infrastructure, and the project "does not operate Vigil as a service". `/api` and `/openapi.json` on the site return 404; there is no public API, feed, STIX or CSV export, no site rate limits, and `robots.txt` allows all crawling. So there is nothing on vigilsoc.org to build an MCP client against, and this plugin makes **no requests to vigilsoc.org**.
- **The real integration surface is your own Vigil's `/mcp` endpoint.** Vigil's official plugin manifest (`clients/claude-code/.claude-plugin/plugin.json` in the Vigil repo) uses `type: http`, URL `<vigil>/mcp`, header `Authorization: Bearer <credential>`. This plugin uses the same contract. The surface is off until an operator opens it.
- **Because the official plugin already is the MCP bridge, this plugin ships no server code** (so no dependencies to pin). Its value is the defender layer on top: workflows, a read-only subagent, and a fail-closed confirmation hook.

### Components

| Component | Purpose |
|---|---|
| `.claude-plugin/plugin.json` | Manifest; inline `vigil` MCP server; `userConfig` for URL + sensitive credential |
| `skills/soc-context` | Auto-loading rules: tool map, untrusted-data handling, confirmation, privacy, output discipline |
| `skills/triage`, `enrich`, `detect`, `ir-brief` | User-invoked commands (`/vigil-defender:<name>`); read-only |
| `agents/investigator.md` | Subagent whose `tools` list contains only read-only Vigil tools |
| `hooks/hooks.json` + `scripts/vigil_guard.py` | `PreToolUse` hook: any Vigil tool not on a read-only allowlist triggers a confirmation prompt (fails closed on unknown tools) |
| `tests/test_vigil_guard.py` | Unit tests for the guard and manifest invariants |

## Install

Requires Claude Code >= 2.1.323, `python3` on PATH (guard hook, stdlib only), and a running Vigil.

1. **Open the MCP surface and mint a credential in Vigil** (documented in Vigil's own client README): Settings → Integrations → *Vigil MCP server*, mint a credential; or on the Vigil host `python -m core.auth.mcp_token <username>`. The credential acts with that user's Vigil role and is audited under them.
2. Install:
   ```bash
   claude plugin marketplace add /path/to/this/repo      # or its git URL
   claude plugin install vigil-defender@vigil-defender-marketplace
   ```
   Claude Code prompts for **Vigil address** (e.g. `http://localhost:6987`, no `/mcp`) and **credential**. The credential is stored in the OS secure store (`sensitive: true`); nothing is in any file here. Reconfigure via `/plugin`.
3. Try it without installing: `claude --plugin-dir /path/to/this/repo`.

**Don't enable both `vigil-defender` and the official `vigil-soc` plugin**: each registers a `vigil` MCP server with the same tools. Use this one, or use `vigil-soc` and copy only the skills/agent/hook (the hook matcher `mcp__plugin_vigil.*` already covers both names; the agent's `tools` names assume this plugin's name).

Use plain HTTP only for loopback or an SSH tunnel; use HTTPS otherwise.

## Usage

```
/vigil-defender:triage high 10
/vigil-defender:enrich <finding-id or entity>
/vigil-defender:detect T1059.001
/vigil-defender:ir-brief <case-id>
```
Or ask naturally ("what's hot in Vigil today?"); `soc-context` loads. For a deep dive: "use the investigator agent on case X".

## Safety model

- **Untrusted input.** Findings, notes, comments and IOCs can be attacker-written. Skills and the agent instruct Claude to treat them strictly as evidence, quote embedded instructions rather than follow them, and never fetch/run/send because data said so. This is a mitigation, not a guarantee; keep a human in the loop.
- **No side effects without confirmation.** The hook asks before *every* Vigil tool not on its read-only list (create/update/close case, start run, directives, approvals, and any tool added in a future Vigil). A hook `ask` overrides `allowed-tools`. The subagent has no write tools at all. Containment is only ever proposed; approval is a human act in Vigil (Vigil itself leaves `approve_action` pending over `/mcp`).
- **Privacy / external services.** The plugin talks only to the Vigil address you configure. Tool results do enter the Claude Code session and therefore reach whichever model provider you use (for most, Anthropic); route Claude Code to an in-boundary provider if that matters. No command calls any third-party intel service; the skills require explicit per-item opt-in before any IOC from a private incident leaves your environment. No telemetry.
- **Credentials.** `userConfig` with `sensitive: true`; never hardcoded. Prefer a token for a least-privilege Vigil user; it can be revoked in Vigil at any time.
- **Rate limiting / caching.** The plugin has no network code of its own. Gentle use is enforced by instruction: one page (<= 25) at a time, reuse of results already in the session, no polling loops. Vigil documents auth-route limits (5/min); it states no limit for `/mcp` tools. Real throttling and caching are Vigil's job; none is implemented here.

## Test and validate

```bash
python3 -m unittest discover -s tests -v     # guard logic + manifest invariants
claude plugin validate . --strict            # manifest/layout check
```

## Limitations and unverified items

- **`/mcp` tool list.** Names come from Vigil's API docs and `tools/mcp/vigil.py` as read on 2026-11-18, not from a live instance. Some are marked frozen upstream; others may change ("0.x terms"). I could not call a live server, so tool argument details (e.g. for `get_entity_facts`) are deliberately left to the tool schema. Pin the plugin to your Vigil version and check `tools/list`.
- **Hook allowlist.** `READ_ONLY` in `scripts/vigil_guard.py` is a judgment from names/docs; verify against your version. Wrong direction is safe: an unlisted read just prompts.
- **No coverage of Vigil's REST API** (cases/SLA/ATT&CK endpoints exist, but token acquisition for them is not documented in the pages I read) or of Vigil's backend integration tools (VirusTotal, Splunk, Elastic...), which are not documented as exposed over `/mcp`.
- **Detection rules.** Vigil ships none; `detect` only reads `~/security-detections/` if you cloned it with Vigil's script, and only drafts.
- Docs inconsistencies noted upstream (version 1.0.0 vs 1.1.x, `/mcp` auth header not stated on the docs site). Claude Code plugin schema was read from the official docs on the same date; hooks/MCP-in-plugin details came from partially summarized pages.
- LLM output may be wrong. Vigil's terms require review by a qualified person for consequential actions.

## License

MIT (see `LICENSE`). Choose another if you prefer.
