# vigil-defender: research, design and status notes

Last updated 2026-11-18. Complements `README.md` (user-facing) with the research trail, decisions, assumptions and open items.

## 1. Research findings

### vigilsoc.org (fetched 2026-11-18)
- Project website for **Vigil**, an open-source (Apache-2.0) agentic AI SOC maintained by DeepTempo.AI and supporters. Self-hosted: Terms s.2 "We do not operate Vigil as a service."
- **No public API.** `/api` and `/openapi.json` return 404. No RSS/Atom/JSON feed, STIX or CSV found. No site rate limits stated.
- `robots.txt`: `User-agent: *` / `Allow: /`, named AI crawlers (incl. ClaudeBot, Claude-User) allowed, no `Disallow`, no `Crawl-delay`.
- Terms (effective 2026-11-16): you secure your own instance and credentials; no circumventing approval controls; do not present software as an official product of Anthropic or another company; no trademark rights to "Vigil"; LLM output must be reviewed by a qualified person.
- Privacy (effective 2026-11-15): no telemetry; data goes only to the operator's Vigil and the model provider; `/mcp` is closed until an operator opens it and accepts only minted credentials.
- Contacts: security@deeptempo.ai; `/.well-known/security.txt` (RFC 9116).

### Vigil's real integration surface
- Local API (FastAPI) on port 6987 (`/docs`, `/api/health`), web console on 6988.
- REST auth: `Authorization: Bearer <token>`; token issuance for REST is **not documented** on the pages read. Auth routes limited to 5/min; no other limits stated.
- MCP tools documented on the API page: `get_finding`, `list_findings` (limit max 25), `get_technique_rollup`, `create_case`, `update_case`, `add_finding_to_case`, `list_cases`, `get_case`, `create_approval_action`, `list_approval_actions`. Source (`tools/mcp/vigil.py`) and the official plugin add: `close_case`, `get_case_iocs`, `get_case_comments`, `get_entity_facts`, `list_workflows`, `start_run`, `get_run`, `send_directive`, `list_operator_agents`, `list_completed_hunts`, `reject_action`, `approve_action` (stays pending over MCP), `list_audit_entries` and others.
- **Transport/auth of `/mcp`** is not stated on the docs site. It was taken from the official plugin manifest (`clients/claude-code/.claude-plugin/plugin.json` in `Vigil-SOC/vigil`): `type: http`, `url: <vigil_url>/mcp`, header `Authorization: Bearer <credential>`, credential marked `sensitive`.
- Credential minting: Settings > Integrations > *Vigil MCP server*, or `python -m core.auth.mcp_token <username>` on the Vigil host. It acts with that person's role and is audited under them.
- Detection rules are not shipped; `scripts/setup_detection_repos.sh` clones public repos to `~/security-detections/`.
- Upstream inconsistencies: site banner says 1.0.0, docs mention 1.1.x; README `./start.sh -d` vs docs `--daemon`.

### Claude Code plugin docs (code.claude.com, read 2026-11-18)
Used for: `.claude-plugin/plugin.json` schema (only `name` required), `userConfig` with `sensitive`, `${user_config.KEY}` substitution in MCP config, `skills/<name>/SKILL.md` (preferred over `commands/`), `agents/*.md` (plugin agents ignore `mcpServers`/`hooks`/`permissionMode`), `hooks/hooks.json` with top-level `"hooks"`, MCP tool names `mcp__plugin_<plugin>_<server>__<tool>`, `claude plugin validate`. Hooks and MCP-in-plugin pages were only partially retrieved (summarised); the validator accepted the result.

## 2. Design decisions
- **No custom MCP server.** Nothing on vigilsoc.org to wrap, and Vigil's own `/mcp` plus the official `vigil-soc` plugin already provide programmatic access. Hence no server code and no dependencies to pin.
- **Components that earn their place:** `soc-context` skill (rules), four user-invoked read-only skills (triage, enrich, detect, ir-brief), a read-only `investigator` subagent, and a fail-closed `PreToolUse` hook.
- **Hook policy:** silent only for allowlisted read-only tools; everything else, including unknown tools and unparseable input, gets `permissionDecision: ask`. Matcher `mcp__plugin_vigil.*` also covers the official `vigil-soc` plugin.
- **Untrusted data:** all retrieved content is evidence, never instruction; no fetching/running/sending because data said so.
- **Privacy:** only the configured Vigil URL is contacted; no third-party intel lookups without per-item opt-in; tool results do reach the user's model provider (stated in README).
- **Rate limiting/caching:** no network code in the plugin; guidance only (one page <= 25, reuse results, no polling). Real throttling is Vigil's responsibility.
- **Name/branding:** `vigil-defender`, labelled unofficial, to respect Terms s.4/s.5.

## 3. Verification status
- `claude plugin validate . --strict`: passed (warnings only: no icon, no `privacyPolicyUrl`).
- `python3 -m unittest discover -s tests -v`: 9 tests pass (guard logic, fail-closed behaviour, no hardcoded secret, agent tools subset of read-only list).
- **Not yet tested against a live Vigil.** Tool names/args are from docs and source, not `tools/list`.

## 4. Where the official `vigil-soc` plugin is (checked 2026-11-18)
- **Source:** `clients/claude-code/` in `github.com/Vigil-SOC/vigil`, listed by the repo's root `.claude-plugin/marketplace.json` (marketplace name `vigil`). Current manifest version 1.1.3; skills: status, queue, case, investigate, hunt, waiting, trail.
- **Not installed in this workspace.** `claude plugin list` shows only `security-guidance@synced` and `engineering@synced`; configured marketplaces are only `anthropic-plugin-directory` and `claude-plugins-official`; `claude mcp list` has no Vigil entry. So it cannot be used here yet.
- **Install** (pin to your Vigil version tag):
  ```bash
  claude plugin marketplace add Vigil-SOC/vigil@v<your Vigil version> --sparse .claude-plugin clients/claude-code
  claude plugin install vigil-soc@vigil
  ```
  Claude Code then prompts for the Vigil address and credential. Run interactively (or via `/plugin`) so the token is stored in the secure store rather than shell history.
- **Alternative:** install `vigil-defender` from this folder (`claude --plugin-dir .`). Do not enable both; they register the same `vigil` server.

## 5. Assumptions and open items
1. Tool names/schemas may differ by Vigil version (some are "0.x terms" upstream).
2. `READ_ONLY` allowlist is a name-based judgment; a wrong entry errs toward an extra prompt.
3. Token acquisition for the REST API is undocumented; REST is not used.
4. Vigil backend integration tools (VirusTotal, Splunk, Elastic, ...) are not documented as exposed over `/mcp`; not used.
5. License chosen: MIT (changeable). Author field is generic.
6. Optional before publishing: add icon (square PNG 512-2048 px) and a plugin-specific privacy policy URL.
7. LLM output may be wrong; consequential actions need review by a qualified person.
