# The Claude Code client for Vigil

Work your team's Vigil, the open source AI SOC you run yourself, from a Claude Code session.

It works in Claude Code: the terminal, the IDE extensions, and the Code tab of the Claude desktop app. In the Claude chat app the skills load but have no connection to Vigil, because chat reaches a server only at a fixed public address and your Vigil has none.

## What it adds to Claude Code

Claude Code alone can reason about a log you paste into it. It holds no cases, no record of last week's hunt, and no view of what a colleague concluded. Vigil holds those, and this client brings them into the session:

- **A SOC behind the session.** Investigations and hunts run in Vigil, on its agents and the integrations your Vigil already has, not in the session's context. The session starts a run, steers it, and reads its conclusion with citations and declared gaps.
- **Work shared with colleagues.** Cases, hunts, runs and verdicts live in Vigil. A colleague opens the same case from their own session, reads what you concluded, and builds on it.
- **Memory across runs.** A new run begins with what earlier runs concluded about the same entities, whoever started them, within the limits of restricted cases.
- **Your controls.** Each call runs under the analyst's own credential and role and is recorded on Vigil's audit trail under that person. A session cannot approve an action; approval stays with a person in the console.

## What it is

A Claude Code plugin, named `vigil-soc`, that works your own Vigil from a Claude Code session. It holds no logic: an MCP entry that points at your Vigil's `/mcp` surface under a credential you minted, and seven short skills.

Vigil's kernel does the work. The session starts investigations and hunts, steers them and reads what they conclude, with their citations and declared gaps. Vigil enforces role, the autonomy ceiling and approvals, and records every call on its audit trail under the credential's person, the same as the console.

| Skill | What it does | Tools it uses |
|---|---|---|
| `/vigil-soc:status` | Does Vigil answer for this credential | `list_findings` |
| `/vigil-soc:queue` | Open findings and cases | `list_findings`, `list_cases` |
| `/vigil-soc:case <id>` | One case and its evidence | `get_case`, `get_case_iocs`, `get_case_comments` |
| `/vigil-soc:investigate <case id>` | Start an investigation run, steer it, read its conclusion once it ends | `list_workflows`, `list_operator_agents`, `start_run`, `send_directive`, `get_run` |
| `/vigil-soc:hunt <case id> <hypothesis>` | Start a hunt the same way | the same, and `list_completed_hunts` |
| `/vigil-soc:waiting` | What waits on a person, and reject one | `get_run`, `list_approval_actions`, `get_approval_action`, `reject_action` |
| `/vigil-soc:trail` | The audit trail | `list_audit_entries` |

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

The credential acts as its person, with that person's role. Reading the trail (`/vigil-soc:trail`) needs an Admin, Manager or auditor; an analyst's credential is refused it.

## Install

Pin the marketplace to the tag of the Vigil you run, so the skills match its tools:

```bash
claude plugin marketplace add Vigil-SOC/vigil@v<your Vigil version> --sparse .claude-plugin clients/claude-code
claude plugin install vigil-soc@vigil
```

`--sparse` fetches only the marketplace file and this directory, not the whole repository. From a local checkout, `claude plugin marketplace add /path/to/vigil` works the same way.

Claude Code then asks for two values: your Vigil's address (for example `http://localhost:6987`, without `/mcp`) and the credential. The credential is marked sensitive, so Claude Code keeps it in the platform's secure store. No file in the plugin holds an address or a secret. Change either later with `/plugin` → vigil-soc → configure, then run `/vigil-soc:status`.

## Sign in with OAuth instead of a token

If your Vigil has OAuth turned on for the surface ([OAuth on the MCP surface](../../docs/mcp-oauth.md)), Claude Code can connect from the URL alone: it registers, opens Vigil's consent page in your browser, and keeps the tokens it is given. There is no credential to mint or paste.

The plugin's entry always sends the credential as an `Authorization` header, and Claude Code does not attempt OAuth for a server whose `Authorization` header is set, even to an empty value. So for OAuth:

1. Install the plugin as above for its skills, and leave the credential unset. Without it, the plugin starts no server of its own.
2. Add Vigil's server yourself, without a header, and sign in:

   ```bash
   claude mcp add --transport http vigil https://vigil.example.com/mcp
   claude mcp login vigil   # or /mcp in a session → vigil → Authenticate
   ```

3. Approve "Connect Claude Code (vigil)?" on Vigil's consent page. `claude mcp list` then shows `vigil` as connected, and the skills use it.

The connection appears in Vigil under your API tokens, marked "Connected app", and ends when you revoke it there. A personal token remains the way in for headless and scripted use.

## What it cannot do

**Approve from the session.** Over `/mcp`, `approve_action` leaves the action pending, and `send_directive` with `approve`, `reject` or `benign` does not answer a run's checkpoint. Approving happens in Vigil's Approvals. The credential is a `vgl_api_` API token, though, and it also reaches Vigil's HTTP API as its holder: if that person holds `ai_decisions.approve`, `POST /api/v1/approvals/{id}/approve` with the token approves the action. Four-eyes still applies. A proposal from the session (`create_approval_action`) is recorded as that person's, so with four-eyes on, someone else must approve it. Rejecting works from the session.

## Privacy: where the data goes

Tool results enter the Claude Code session, and so reach whichever model provider that session uses. For many setups that is a provider outside your environment. If you route Claude Code to a model inside your own boundary, the data stays inside it end to end. Nothing calls DeepTempo and nothing is hosted: the plugin talks only to the Vigil address you enter.

The surface is off until an operator opens it, and an operator can close it or revoke a credential at any time.

The full statement is the project's [privacy policy](https://vigilsoc.org/privacy/), kept in this folder as [PRIVACY.md](PRIVACY.md); use of the plugin is under the [terms of use](https://vigilsoc.org/terms/), kept as [TERMS.md](TERMS.md).
