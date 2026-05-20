# Lab 03 — Run a Multi-Agent Workflow

**Time:** 45 minutes
**Goal:** Execute the `incident-response` workflow end-to-end on a critical finding. Watch four agents hand off to each other, read the final report, and identify where human approval gates would (and would not) fire.

---

## Prerequisites

- Lab 00 + 01 + 02 complete.
- `ANTHROPIC_API_KEY` set.

---

## The workflow you'll run

**`incident-response`** ([workflows/incident-response/WORKFLOW.md](../../workflows/incident-response/WORKFLOW.md)) — Vigil's flagship multi-agent playbook, modeled on NIST IR. Four phases:

1. **Triage** — rapid severity assessment.
2. **Investigator** — deep root-cause analysis, timeline reconstruction, evidence chain.
3. **Responder** — proposes containment actions (host isolation, account disable, firewall block, etc.). Many of these require human approval before execution.
4. **Reporter** — produces an audit-ready incident report.

The interesting parts for an evaluator are **the handoffs** and **the approval gates**, not just the final report.

---

## The finding you'll use

`finding-003` — C2 Beacon Communication. Critical severity. Outbound connection from `10.0.5.120` to `185.220.101.5:443` every 60 seconds. MITRE T1071.001 + T1573.001.

This is a "ransomware-y" finding — the kind that should produce a full workflow run with meaningful containment recommendations. If it doesn't, that's a finding (pun intended).

Ingest the sample fixture if you haven't:

```bash
curl -X POST http://localhost:6987/api/findings/ \
  -H 'Content-Type: application/json' \
  -d @tests/fixtures/sample_findings.json
```

---

## Steps

### 1. List available workflows (2 min)

```bash
curl -s http://localhost:6987/api/workflows | jq '.[] | {id, name, description}'
```

You should see at minimum: `incident-response`, `full-investigation`, `threat-hunt`, `forensic-analysis`, `cloud-incident`. These are loaded from `workflows/*/WORKFLOW.md` at startup.

### 2. Read the workflow definition (5 min)

Open [workflows/incident-response/WORKFLOW.md](../../workflows/incident-response/WORKFLOW.md). Note:

- The YAML frontmatter: name, agents, tools-used, use-case, trigger-examples.
- The phase definitions — purpose, tools, steps, what each agent should hand to the next.

This file is the entire workflow definition. There's no DAG editor, no DSL — it's markdown the workflow service parses ([services/workflows_service.py](../../services/workflows_service.py)).

**Important:** This means engineers on your team could read, review, and modify workflows directly in Git. That's the extensibility story for workflows. Decide whether that's a feature or a limitation for your team.

### 3. Execute the workflow (15 min)

**Via API** (recommended — easier to watch):

```bash
curl -X POST http://localhost:6987/api/workflows/incident-response/execute \
  -H 'Content-Type: application/json' \
  -d '{
    "finding_id": "finding-003",
    "context": "Critical C2 beacon detected. Investigate and propose containment."
  }' | jq
```

The response includes a `run_id`. Poll it to watch progress:

```bash
# Replace RUN_ID with the actual id from the previous response
watch -n 3 'curl -s http://localhost:6987/api/workflows/runs/RUN_ID | jq "{status, current_phase, phases_completed}"'
```

A full IR workflow takes 1–5 minutes depending on model speed.

**Via UI** (alternative): open the C2 finding in the Investigation view and look for a "Run Workflow" or "Incident Response" button.

### 4. Read the phase-by-phase output (15 min)

Once the run completes, fetch the full result:

```bash
curl -s http://localhost:6987/api/workflows/runs/RUN_ID | jq
```

For each phase, capture in your notes:

- **Triage output:** Did it match the verdict from Lab 02's style of triage? Faster or slower?
- **Investigator output:** What evidence did it pull? Did it reconstruct a timeline? Did it identify the C2 destination as known-bad, or just suspicious?
- **Responder output:** **This is the important one.** What containment actions did it propose? For each action:
  - Is it appropriate to the finding?
  - Is it *reversible* or destructive?
  - Did the agent flag whether the action requires approval, or did it propose it as auto-executable?
- **Reporter output:** Is the report audience-appropriate (technical, executive, both)? Is it complete enough that you could send it to your boss without rewriting?

### 5. Trace the handoffs (5 min)

Open the **AI Decisions** page in the UI. Filter by your workflow's run ID or the time window. You should see four entries — one per phase. For each, look at:

- The system prompt the agent ran with.
- What the previous phase passed in as context.
- What this phase passed out.

**Handoff quality** is the most-underrated part of multi-agent systems. Ask: did each phase actually use what the previous phase produced, or did it re-analyze from scratch?

### 6. Find the approval gates (3 min)

If the Responder proposed any destructive action (host isolation, account disable, firewall block), find where approval would be enforced:

- In the UI: a pending-approval queue, an "Approve" button on a proposed action, or a notification.
- In code: [services/approval_service.py](../../services/approval_service.py).

If no approval gate appeared even though the Responder proposed destructive actions — **that's a finding to capture.** Production deployments need this.

---

## Success criteria

- [ ] The workflow completed without error (status: `completed` or `success`).
- [ ] You read all four phase outputs and captured at least one observation per phase.
- [ ] You identified at least one handoff that was tight (the next phase clearly built on the prior) and at least one that was loose or redundant.
- [ ] You located the approval gate mechanism — either by seeing one fire in the UI, or by reading [services/approval_service.py](../../services/approval_service.py).
- [ ] You can name one situation where you would trust this workflow to run autonomously, and one where you wouldn't.

---

## Findings to capture

1. **Time-to-report:** wall-clock from "execute" to a complete report. Acceptable for your SOC's tempo?
2. **Containment quality:** did the Responder propose the right actions for a C2 beacon? Anything missing?
3. **Approval risk:** would running this workflow autonomously in production have caused damage on this finding? On what kind of finding *would* it?
4. **Handoff failure mode:** is there a phase that ignored prior context or re-did work?
5. **Workflow definition usability:** could a security engineer on your team modify `WORKFLOW.md` confidently? Or is the markdown-as-DSL surprising / fragile?

---

## Where to look next

- All four workflow definitions are siblings of [workflows/incident-response/WORKFLOW.md](../../workflows/incident-response/WORKFLOW.md). Skim at least one other.
- Workflow service code: [services/workflows_service.py](../../services/workflows_service.py).
- Approval mechanism: [services/approval_service.py](../../services/approval_service.py).
- Workflow run state model: [docs/STATE.md](../STATE.md).

→ Continue to **[Lab 04 — Critical Evaluation](04-critical-evaluation.md)**.
