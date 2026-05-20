# Lab 02 — Triage a Single Finding

**Time:** 45 minutes
**Goal:** Fire one AI agent (Triage) at one specific finding, read its reasoning, and form a judgment on whether the verdict is justified.

This is the load-bearing exercise for "is the AI any good?". Take it seriously — your answer here is most of what your team needs from this evaluation.

---

## Prerequisites

- Lab 00 + 01 complete.
- `ANTHROPIC_API_KEY` set — this lab actually calls Claude.

---

## The finding you'll triage

We're using a deterministic finding from `tests/fixtures/sample_findings.json` so everyone evaluating Vigil discusses the *same* example.

**Finding ID:** `finding-001` — Suspicious PowerShell Execution
- Severity: high
- Source: Splunk (notable event 12345)
- Host: `workstation-042`, user `john.doe`
- Command: `powershell.exe -enc JABhAD0AJwBoAHQAdABwADoALwAvAG0AYQBsAHcAYQByAGUALgBjAG8AbQAn`
  - (That base64 decodes to `$a='http://malware.com'` — a contrived but plausibly suspicious PowerShell payload.)
- MITRE: T1059.001 (PowerShell), T1027 (Obfuscated Files or Information)

This finding is intentionally **suspicious-looking but ambiguous** — the kind of thing your triage agent will face every day. Don't peek at any agent output yet. Form your own opinion first.

---

## Step 1 — Form your own triage call (5 min, no Vigil)

Before running the AI, write down your own verdict. On a sticky note or notes app:

1. **Severity:** Critical / High / Medium / Low / False Positive
2. **Confidence:** High / Medium / Low
3. **Next step:** Escalate to IR / Investigate further / Close as benign / Need more data
4. **The one piece of evidence that drove your call.**

Don't deliberate for long — 60 seconds, the way you would on shift.

---

## Step 2 — Make sure the finding exists in your Vigil instance

In demo mode you may not have `finding-001` specifically. Ingest the sample fixture so it's there:

```bash
# From the repo root
curl -X POST http://localhost:6987/api/findings/ \
  -H 'Content-Type: application/json' \
  -d @tests/fixtures/sample_findings.json
```

Then confirm:

```bash
curl -s http://localhost:6987/api/findings/finding-001 | jq '.title, .severity, .mitre_techniques'
```

If demo mode rejects the POST, you have two options:
- (a) Use any high-severity demo finding instead — note its ID and substitute in the steps below.
- (b) Temporarily disable demo mode (`DEMO_MODE=false` in `.env`, restart) and re-POST. The labs are easier with demo on, so prefer (a).

---

## Step 3 — Triage via the UI (15 min)

1. Open `http://localhost:6988`, navigate to **Findings**.
2. Find your target finding and click into the detail view.
3. Trigger the **Triage** agent — typically a button labelled "Investigate", "Triage", or "Run Agent" on the finding detail page.
4. Watch the agent execute. Note:
   - How long does it take?
   - Does it stream reasoning, or just return a final verdict?
   - Which MCP tools does it call (if visible in the UI)?
5. When it finishes, read the full output. Capture in your notes:
   - The agent's severity verdict.
   - The agent's confidence (if expressed).
   - The agent's reasoning chain — what evidence did it cite?
   - Any MITRE techniques the agent mapped (and whether they match the original finding's mapping).
   - Any recommended next steps.

---

## Step 4 — Triage via the API (10 min)

Now do it again via the API, so you see the raw response shape:

```bash
curl -X POST http://localhost:6987/api/claude/agent/task \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "triage",
    "task": "Triage finding finding-001. Assess severity, confidence, and recommended next step. Use the get_finding tool to fetch full details.",
    "max_turns": 8
  }' | jq
```

This hits [backend/api/claude.py:904](../../backend/api/claude.py#L904) → `services/claude_service.py` → the Triage agent prompt defined in [services/soc_agents.py](../../services/soc_agents.py).

Read the response. Note:

- `tool_calls` — which MCP tools did the agent invoke, and in what order?
- `result` — the final reasoning chain. Is it the same as the UI output? Different formatting? Different conclusions?
- Time and tokens — how long did the agent take, how many turns?

Capture the verdict here too.

---

## Step 5 — Compare and judge (15 min)

This is the actual eval. Open three things side by side:

1. Your sticky-note triage call from Step 1.
2. The agent's UI output from Step 3.
3. The agent's API output from Step 4.

Answer in your notes:

1. **Did the agent reach the same conclusion you did?** If not, which one of you is right? Why?
2. **Was the agent's reasoning chain transparent enough that you could audit it?** Could you defend its verdict to your SOC manager?
3. **What data did the agent *not* have access to** that would have changed your own call? (E.g., process tree, user history, network context, threat intel on the payload URL.) Did the agent acknowledge those gaps, or speak past them?
4. **Did the agent's MITRE mapping match the original finding's?** If different, which is more accurate?
5. **Overconfidence test:** read the agent's verdict like a defense attorney. Where did it state something as fact that was actually inference?

You'll come back to these answers in Lab 04.

---

## Success criteria

- [ ] You ran the Triage agent against `finding-001` (or your substitute) both via UI and API.
- [ ] You captured the agent's verdict, reasoning, MITRE mapping, and tool calls.
- [ ] You documented one place where the agent's reasoning was **justified by the evidence shown** and one place where it was **not** (or where you needed more data to tell).
- [ ] You can name the system prompt file ([services/soc_agents.py](../../services/soc_agents.py)) the Triage agent runs under, and have at least skimmed the Triage entry there. (Transparency builds trust. If you can read the agent's instructions, you can decide whether to trust its outputs.)

---

## Findings to capture

1. The agent's verdict vs. yours — which was right?
2. One concrete strength of the agent's reasoning.
3. One concrete weakness or gap.
4. Whether you'd let this agent triage alerts without human review at your org. Yes/no, and why.

---

## Where to look next

- Triage agent role + methodology: [docs/AGENTS.md](../AGENTS.md) → Triage section.
- Triage agent's system prompt: [services/soc_agents.py](../../services/soc_agents.py) — search for `"triage"` in `AGENT_CONFIGS`.
- Agent task API schema: `http://localhost:6987/docs` → `POST /api/claude/agent/task`.

→ Continue to **[Lab 03 — Run a Multi-Agent Workflow](03-run-a-workflow.md)**.
