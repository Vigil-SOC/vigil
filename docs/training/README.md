# Vigil "Kick the Tires" — Half-Day Lab Series

A structured, hands-on evaluation track for **security engineers** sizing up Vigil for SOC use. Self-paced, ~3.5 hours total (plus a 30-minute optional stretch).

By the end of these labs, you will be able to answer for your team:

1. What is Vigil's analyst workflow, end-to-end?
2. How does a Vigil AI agent reason over a single security finding?
3. Where do the multi-agent workflows shine, and where do they need supervision?
4. What's logged in the audit trail, and what does it cost to run?
5. What's the integration story, and what's missing for *your* environment?

These labs do **not** cover building new agents or MCP integrations — that's a separate "developer extensibility" track. If you find yourself wanting to extend rather than evaluate, skip to [docs/AGENTS.md](../AGENTS.md), [docs/INTEGRATIONS.md](../INTEGRATIONS.md), and the `CLAUDE.md` at the repo root.

---

## Time budget

| Lab | Topic | Time | Cumulative |
|---|---|---|---|
| [00](00-setup.md) | Stand up Vigil locally | 30 min | 0:30 |
| [01](01-analyst-workspace-tour.md) | Analyst workspace tour | 30 min | 1:00 |
| [02](02-triage-a-finding.md) | Triage a single finding | 45 min | 1:45 |
| [03](03-run-a-workflow.md) | Run a multi-agent workflow | 45 min | 2:30 |
| [04](04-critical-evaluation.md) | Critical evaluation: false-positive hunt | 30 min | 3:00 |
| [05](05-audit-trail-and-cost.md) | Audit trail & cost | 30 min | 3:30 |
| [06](06-stretch-real-integration.md) | (Stretch) Connect a real integration | +30 min | 4:00 |

Labs 00–05 are the core track. Lab 06 is optional and is for engineers who finish early or who specifically want to validate the real-integration path before recommending Vigil.

---

## Prerequisites

Before you start Lab 00, confirm:

- [ ] **Docker Desktop** is installed and running (Vigil uses Docker for Postgres + Redis).
- [ ] **Python 3.10+** (`python3 --version`).
- [ ] **Node.js 18+** (`node --version`).
- [ ] **Git** with submodule support.
- [ ] **An Anthropic API key** in an environment variable: `export ANTHROPIC_API_KEY=sk-ant-...`.
  - Without this, the AI agent labs (02, 03, 04, 06) will not function. Labs 00, 01, and 05 work without it.
- [ ] A working terminal and a modern browser.

You do **not** need Splunk, CrowdStrike, or any other security tool credentials for Labs 00–05. The labs use Vigil's built-in demo mode for synthetic data.

---

## How to run the labs

**Solo, self-paced (default).** Read each lab top-to-bottom, run the commands, check the success criteria, jot answers to the reflection prompts. Don't skip the reflection prompts — they're where the evaluation actually lives.

**Team workshop.** Have everyone do Lab 00 *in advance* (the longest pole on a fresh machine). Reconvene together for Labs 02–04, since the comparison-of-judgment exercises are richer with multiple opinions. Park questions Lab 05 surfaces for a closing discussion.

**Note-taking.** Each lab has a "Findings to capture" block at the end. We recommend a shared doc per evaluator with the same headings so you can compare notes at the end of the day.

---

## What success looks like at the end of the half-day

You should be able to walk a colleague through:

- A live Vigil install with populated demo data.
- One finding triaged by a single agent, and your judgment on the verdict.
- One multi-agent workflow run end-to-end, with the handoffs labelled.
- A documented case where you disagreed with the AI, and *why*.
- A rough estimate of cost per investigation at your expected volume.
- A list of integrations you'd need that Vigil does or does not have today.

That's the deliverable for "can we use this?".

---

## Where to go deeper

Each lab links forward to the relevant reference docs. The full reference set:

- [README.md](../../README.md) — high-level overview, agent + workflow inventory.
- [docs/AGENTS.md](../AGENTS.md) — agent specs, prompts, methodologies.
- [docs/API.md](../API.md) — data model, REST + MCP API.
- [docs/FEATURES.md](../FEATURES.md) — cases, SLA, approvals, exports.
- [docs/INTEGRATIONS.md](../INTEGRATIONS.md) — all 30+ MCP integrations.
- [docs/ARCHITECTURE.md](../ARCHITECTURE.md) — system diagram, data flow.
- [DEV_MODE.md](../../DEV_MODE.md) — dev auth bypass details.
- `CLAUDE.md` at repo root — full developer guide.
