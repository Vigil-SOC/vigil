# Lab 01 — Analyst Workspace Tour

**Time:** 30 minutes
**Goal:** Build an accurate mental model of Vigil's analyst-facing surface, so the later labs are about *judgment* rather than navigation.

This lab is mostly clicking and reading. Resist the urge to invoke agents yet — that's Lab 02.

---

## Prerequisites

- Lab 00 complete: Vigil is running at `http://localhost:6988` with demo data.

---

## The hierarchy you're learning

Vigil's analyst data model is a three-tier hierarchy:

```
Finding   ──→   Case   ──→   Investigation
(an alert from   (an analyst's   (an AI-driven deep
 a SIEM/EDR)     working unit)    dive on the case)
```

- **Findings** come from external tools (Splunk, CrowdStrike, Sentinel, Defender, etc.) via the ingest pipeline.
- A **Case** groups one or more related findings into an analyst's working unit, with SLA, owner, status, timeline.
- An **Investigation** is what the AI does — one or more agent runs producing reasoning, evidence, and recommended actions, attached to a case or a finding.

The audit trail of *what the AI decided and why* is its own first-class page (**AI Decisions**), separate from the case timeline. That separation is intentional and important to understand.

---

## Steps

Walk these pages in order. For each, give yourself 2–3 minutes. At each stop, answer the three "look for" questions in your notes.

### Stop 1: Dashboard (`/`)

The landing page. Health summary, recent activity, metrics tiles.

- **Look for:** Where would your eye go in an actual on-shift moment?
- **Look for:** What's missing that you'd want?
- **Look for:** Anything that looks misleading (e.g., a metric that's flat or hard-coded in demo mode)?

### Stop 2: Findings (`/findings`)

The raw alert stream. List view with filtering, search, severity badges.

- **Look for:** What columns are present? What's missing?
- **Look for:** Click a finding. What's on the detail view — fields, related items, MITRE techniques?
- **Look for:** The "Investigate" / agent-trigger affordance. Don't click it yet — note where it is.

### Stop 3: Cases (`/cases`)

Analyst working units. Each case has status, SLA, owner, attached findings, timeline.

- **Look for:** How is a case created from findings? Look for "Create Case" or "Add to Case" actions.
- **Look for:** Click into a case. What's on the timeline? Notes, status changes, agent runs, approvals?
- **Look for:** SLA indicators — green/yellow/red, time remaining. See [docs/SLA_QUICK_REFERENCE.md](../SLA_QUICK_REFERENCE.md) if you want the full SLA model.

### Stop 4: Investigation (open one from a finding or case)

The AI-driven deep dive. This is where chat-with-the-AI, entity graphs, and agent runs live.

- **Look for:** The chat affordance — can you ask the AI a question about this finding?
- **Look for:** Entity graph or related-findings view.
- **Look for:** "Run agent" or "Run workflow" buttons. Where are they? What options do they offer?

### Stop 5: AI Decisions (`/ai-decisions`)

The audit trail. Every agent invocation, its inputs, outputs, tool calls, and confidence.

- **Look for:** Filter by agent, by finding, by time. What axes are filterable?
- **Look for:** Click into one decision. What's logged — full prompt, model used, tokens, tool calls?
- **Look for:** Is there an approval/rejection action visible from here?

### Stop 6: Analytics + Cost Analytics (`/analytics`, `/cost-analytics`)

SOC metrics (MTTR, kill-chain coverage, case resolution time) and LLM spend tracking.

- **Look for:** Which charts feel like SOC manager dashboards vs. eng-only?
- **Look for:** Cost breakdown — by agent? by integration? by case? by day?
- **Look for:** Anything that's clearly demo-mode placeholder data vs. real metrics.

### Stop 7: Settings (`/settings`)

Configuration: API keys, integrations, MCP servers, model selection, auth, users.

- **Look for:** The integrations panel. How many are configured? How many show "available but not connected"?
- **Look for:** Model selection. Per-agent? Global? Tied to cost tier?
- **Look for:** Approval workflow configuration — which actions require human approval before execution?

---

## Success criteria

You're done with Lab 01 when, **without looking at the UI**, you can:

- [ ] Sketch the Finding → Case → Investigation hierarchy on paper.
- [ ] Name 5 of the marquee pages and one thing each is for.
- [ ] Describe where you'd go to answer "did the AI take an action on this finding, and why?"
- [ ] Describe where you'd go to answer "how much have we spent on AI this week?"
- [ ] Identify at least one page you'd want to redesign before showing it to your SOC manager, with a specific reason.

The last bullet is the important one. If everything looked great, you didn't look hard enough.

---

## Findings to capture

1. The page that looks most useful for a real analyst on shift.
2. The page that looks least useful, or most likely to be ignored.
3. Anything that's missing from the analyst flow that you'd consider a dealbreaker.
4. UI affordances that look wrong (buttons that don't make sense, navigation dead ends, confusing terminology).

---

## Where to look next

- Agent specs and methodologies: [docs/AGENTS.md](../AGENTS.md).
- Case management + SLA + approvals: [docs/FEATURES.md](../FEATURES.md).
- Architecture diagram: [docs/ARCHITECTURE.md](../ARCHITECTURE.md).

→ Continue to **[Lab 02 — Triage a Single Finding](02-triage-a-finding.md)**.
