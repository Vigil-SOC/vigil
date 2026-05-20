# Lab 05 — Audit Trail & Cost

**Time:** 30 minutes
**Goal:** Trace one AI investigation end-to-end through the audit log, and estimate what Vigil would cost your organization at your real alert volume.

This lab is what your CISO and your CFO will ask about. Treat it as such.

---

## Prerequisites

- Lab 00–04 complete.
- You've run at least one workflow (Lab 03) and several Triage agents (Lab 02 + 04), so there's data in the audit log.

---

## Part A: Audit trail (15 min)

### 1. Open the AI Decisions page

`http://localhost:6988/ai-decisions`

This page should show every agent invocation across all the labs you've run today. If it's empty, something is wrong — either the page isn't reading from the right data source in demo mode, or interactions aren't being logged. **That itself is a finding to capture.**

### 2. Pick one investigation and audit it

Pick the workflow run from Lab 03 (the C2 beacon incident-response run). You should see four entries — one per phase. Click into each and capture:

- **Inputs:** the system prompt, the task, the context handed in.
- **Model + parameters:** which Claude model? Thinking enabled? Max tokens?
- **Tool calls:** which MCP tools were invoked, with what arguments, returning what?
- **Output:** the final agent response.
- **Tokens:** input tokens, output tokens, thinking tokens.
- **Cost:** dollar amount (if displayed).
- **Latency:** wall clock for the agent run.

### 3. The questions an auditor will ask

For your traced workflow, can you answer:

- [ ] "Show me every action this AI took on this incident." (full tool-call trail)
- [ ] "Show me the prompt it ran with." (verbatim, not paraphrased)
- [ ] "Show me which version of the model was used." (model version matters for reproducibility)
- [ ] "If this AI made a wrong call, how would we know?" (is confidence + reasoning logged in a way that's reviewable later?)
- [ ] "How long do we retain this audit data?" (check [database/init/](../../database/init/) and `LLMInteractionLog` model in [services/models.py](../../services/models.py))

If any of these is *no*, it's a gap to flag — these are baseline requirements for AI in a regulated SOC.

### 4. The data model

Skim the relevant tables in [services/models.py](../../services/models.py):

- `LLMInteractionLog` — the canonical per-agent-call audit record.
- Any `AgentRun` / `WorkflowRun` / `Investigation` models — how runs hang off cases/findings.

You don't need to read the whole file — just confirm the data model makes sense and the columns you'd want for forensics are there (timestamp, agent_id, model, tokens, cost, prompt_hash, tool_calls).

---

## Part B: Cost estimation (15 min)

### 1. Open Cost Analytics

`http://localhost:6988/cost-analytics`

Capture:

- **Total spend today** from your lab work.
- **Cost by agent** — which agents are expensive? Triage should be cheap (small model, 2048-token cap); Investigator and Threat Hunter are expensive (deep thinking, 16k-token cap).
- **Cost by case or workflow** — what does a full IR workflow cost end-to-end?

### 2. Per-run cost benchmarks

From your labs, write down rough numbers:

| Operation | Tokens in | Tokens out | Cost (USD) |
|---|---|---|---|
| One Triage agent run | | | |
| One Investigator agent run | | | |
| One full IR workflow (4 agents) | | | |

If the UI doesn't show these clearly, hit the API:

```bash
# (Endpoint name may vary — check http://localhost:6987/docs → analytics tag)
curl -s http://localhost:6987/api/analytics/cost | jq
```

### 3. Project to your environment

Estimate for your org:

- **Findings per day** that would route to AI triage: __________
- **% that escalate to a full workflow**: __________
- **Therefore per day**: __________ Triage runs and __________ workflow runs.

Math:

```
Daily cost  =  (triage_per_day * triage_cost) + (workflow_per_day * workflow_cost)
Monthly     =  daily * 30
Annual      =  daily * 365
```

Capture all three numbers. Compare against your current SIEM/SOAR spend.

### 4. Cost guardrails

Vigil has cost guardrails in the daemon ([daemon/agent_runner.py](../../daemon/agent_runner.py) and config variables `ORCHESTRATOR_MAX_COST` / `ORCHESTRATOR_MAX_HOURLY_COST`). For autonomous mode, this is what stops a runaway loop from emptying your Anthropic account.

Find:

- [ ] Where are the cost limits configured? ([env.example](../../env.example) and `daemon/config.py`)
- [ ] What happens when the limit is hit — does the daemon pause, alert, or just stop?
- [ ] Is there a per-investigation cost cap, or only a global one?

---

## Success criteria

- [ ] You traced one workflow run end-to-end through the AI Decisions page.
- [ ] You can answer all five "auditor questions" in Part A Step 3 — even if the answer to some is "no, this is a gap."
- [ ] You have rough per-operation cost numbers in Part B Step 2.
- [ ] You have a projected monthly/annual cost for your org's volume in Part B Step 3.
- [ ] You know where the runaway-cost guardrails are configured (Part B Step 4).

---

## Findings to capture

1. **Audit completeness verdict.** Sufficient for a regulated SOC (financial, healthcare, gov)? Yes / No, and what's missing.
2. **Projected annual cost** for your org at expected volume.
3. **Cost-control confidence.** Would you let the daemon run autonomously overnight without supervision, given the current guardrails? Yes / No, why.
4. Any logged data you'd want that *isn't* logged today.

---

## Where to look next

- Audit data model: [services/models.py](../../services/models.py) — `LLMInteractionLog`.
- Daemon cost guardrails: [daemon/agent_runner.py](../../daemon/agent_runner.py), [daemon/config.py](../../daemon/config.py), and the `ORCHESTRATOR_*` variables in [env.example](../../env.example).
- Anthropic public pricing for cost-projection math: https://www.anthropic.com/pricing.

If you have a 30-minute appetite left:

→ Continue to **[Lab 06 — Stretch: Connect a Real Integration](06-stretch-real-integration.md)**.

Otherwise, you're done. Take 10 minutes to consolidate your "Findings to capture" notes from Labs 00–05 into a single document you can share with your team.
