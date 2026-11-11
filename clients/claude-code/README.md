# The Claude Code client for Vigil

A Claude Code plugin, named `vigil`, that works your own Vigil from a Claude Code session. It holds no logic: an MCP entry that points at your Vigil's `/mcp` surface under a credential you minted, and seven short skills.

Vigil's kernel does the work. The session starts investigations and hunts, steers them and reads what they conclude, with their citations and declared gaps. Vigil enforces role, the autonomy ceiling and approvals, and records every call on its audit trail under the credential's person, the same as the console.

| Skill | What it does | Tools it uses |
|---|---|---|
| `/vigil:status` | Does Vigil answer for this credential | `list_findings` |
| `/vigil:queue` | Open findings and cases | `list_findings`, `list_cases` |
| `/vigil:case <id>` | One case and its evidence | `get_case`, `get_case_iocs`, `get_case_comments` |
| `/vigil:investigate <case id>` | Start an investigation run, steer it, read its conclusion once it ends | `list_workflows`, `list_operator_agents`, `start_run`, `send_directive`, `get_run` |
| `/vigil:hunt <case id> <hypothesis>` | Start a hunt the same way | the same, and `list_completed_hunts` |
| `/vigil:waiting` | What waits on a person, and reject one | `get_run`, `list_approval_actions`, `get_approval_action`, `reject_action` |
| `/vigil:trail` | The audit trail | `list_audit_entries` |

## Open the surface and mint a credential

The `/mcp` surface is off on a fresh install. Open it and mint a credential in either of two ways:

- **From the console:** Settings → Integrations, turn on **Vigil MCP server**, then mint a credential for yourself.
- **From the shell**, on the host that runs Vigil, with no console needed:

  ```bash
  # in the Vigil checkout, inside the backend's environment (./start.sh runs it on the host)
  TOKEN=$(venv/bin/python -m core.auth.mcp_token <username>)
  # or, where the backend runs in its container
  TOKEN=$(docker compose -f infra/docker/docker-compose.yml exec -T backend python -m core.auth.mcp_token <username>)
  ```

  This opens the surface if it is closed and prints the token once. Both acts go on the trail.

The credential acts as its person, with that person's role. Reading the trail (`/vigil:trail`) needs an Admin, Manager or auditor; an analyst's credential is refused it.

## Install

Pin the marketplace to the tag of the Vigil you run, so the skills match its tools:

```bash
claude plugin marketplace add Vigil-SOC/vigil@v<your Vigil version> --sparse .claude-plugin clients/claude-code
claude plugin install vigil@vigil
```

`--sparse` fetches only the marketplace file and this directory, not the whole repository. From a local checkout, `claude plugin marketplace add /path/to/vigil` works the same way.

Claude Code then asks for two values: your Vigil's address (for example `http://localhost:6987`, without `/mcp`) and the credential. The credential is marked sensitive, so Claude Code keeps it in the platform's secure store. No file in the plugin holds an address or a secret. Change either later with `/plugin` → vigil → configure, then run `/vigil:status`.

## What it cannot do

**Approve.** An MCP credential cannot release an action. `approve_action` over `/mcp` leaves the action pending, and a run's checkpoint cannot be answered with a directive. Approving happens in Vigil's Approvals with a signed-in person (or `POST /api/v1/approvals/{id}/approve` with a signed-in session). Rejecting works from the session.

## Privacy: where the data goes

Tool results enter the Claude Code session, and so reach whichever model provider that session uses. For many setups that is a provider outside your environment. If you route Claude Code to a model inside your own boundary, the data stays inside it end to end. Nothing calls DeepTempo and nothing is hosted: the plugin talks only to the Vigil address you enter.

The surface is off until an operator opens it, and an operator can close it or revoke a credential at any time.
