# 4. Telemetry reaches the loop through an owned query port, not vendor MCP

Date: 2026-08-07

## Status

Accepted

## Context

An autonomous loop needs to query SIEMs. Vigil already has three ways to
reach Splunk — the official remote MCP server, a first-party MCP server over
`tools/splunk.py`, and a direct-REST federation adapter — plus twenty
first-party Python MCP servers and a set of vendor MCP servers. Reusing them
is the cheapest path to breadth, and the cost concern raised against vendor
MCP was token spend from large tool catalogues.

Auditing the existing paths found the cost concern is not the decisive one.
The decisive findings are about safety and correctness:

- `services/splunk_service.py:128` prepends `search ` to whatever it is
  given. `| delete`, `| outputlookup`, `| sendemail` and `| script` all pass
  through, and a leading `|` breaks out of the intended query entirely.
- `search_by_username:259` and `search_by_hostname:273` interpolate their
  arguments into quoted field expressions with nothing escaping `"` or `|`.
- The approval gate for this is dead code. `ActionType.EXECUTE_SPL_QUERY`
  exists but nothing ever creates one, no query verb appears in
  `tool_manager`'s destructive-verb set, and the unknown tier **fails open**.
- Result size is not bounded at the boundary. `mcp_client.call_tool` returns
  uncapped content; capping happens later by slicing the string at a
  character count, which turns a JSON array into malformed text the model
  cannot reason about and cannot tell has been truncated.

Separately, the schema-discovery problem is unsolved. The DuckDB tool embeds
the entire schema in its tool description, which works for a demo dataset and
will not survive a production SIEM with hundreds of sourcetypes.
`plan.md:418` already identified `describe_sourcetype` as "the single
highest-leverage tool in the build", and it was never built.

## Decision

Every telemetry backend is reached through one owned **query port**: a query
in the backend's native dialect, bounded rows and time, rows out. Read-only
enforcement, source-level row capping, timeout and normalisation live inside
the port, so no caller can opt out of them. A backend that cannot enforce
those does not get a query port.

Workers emit the **native dialect** — SPL, KQL, ES|QL, SQL — validated
against a per-dialect **allow-list of commands, deny-by-default**. An unknown
command is refused, not passed through. This mirrors the existing DuckDB
guard, which permits only `SELECT` and `WITH`.

Schema discovery is part of the port, not the prompt: list the available
sources, describe one, sample from one.

MCP servers are reachable, but only as tools registered through the same
port and allow-listed per role. A role receives the tools it was granted,
never a vendor's full catalogue.

## Consequences

One adapter per SIEM to write and maintain, and one command allow-list per
dialect. The allow-lists are the security-critical part and should be tested
against known-bad queries, not only known-good ones.

Row capping at the source rather than truncation after the fact means the
model always receives valid, complete-but-bounded data. This is a correctness
property, not only a cost one.

Deployment should additionally require a read-only role in the SIEM where the
vendor supports it. The allow-list is the enforceable half of that pair,
because it can be verified from code; the credential half cannot.

Vendor MCP servers remain usable where no native path exists, but never as a
way to bypass the port's guarantees.

## Alternatives considered

**A neutral query IR compiled to each dialect.** Read-only by construction
and one worker prompt for every SIEM. Rejected: the demo's load-bearing
finding rested on a windowed aggregate with `lag()` and `stddev`, and an IR
that covers windowed aggregates, percentiles and cross-domain joins is a real
compiler. Worse, every gap in the IR surfaces as a false visibility gap,
which corrupts a deliverable the product treats as a finding.

**SIEM-side read-only credentials alone.** Robust where supported and no
parser to maintain. Rejected as the sole control: it pushes a security
invariant into per-customer configuration that cannot be verified from code,
and Splunk role capabilities do not cleanly forbid `| sendemail` or
`| script`.
