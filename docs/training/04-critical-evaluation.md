# Lab 04 — Critical Evaluation: False-Positive Hunt

**Time:** 30 minutes
**Goal:** Push the Triage agent against deliberately ambiguous findings, and form a specific opinion on its failure modes.

By design, this lab is **adversarial**. You are not here to confirm that Vigil works. You are here to find where it doesn't, so your team can decide whether the failure modes are acceptable for your environment.

---

## Prerequisites

- Lab 00–03 complete.
- `ANTHROPIC_API_KEY` set.

---

## Why this matters

A SOC AI that is right 80% of the time is dangerous in different ways than one that is right 50% of the time. The 80% one creates *complacency*. Your team will start trusting the verdict and stop reading the reasoning. So the real question isn't "is it accurate?" — it's:

1. When it's wrong, is it wrong in a way you can catch?
2. Does it know when it's uncertain, or does it confidently confabulate?
3. What kinds of findings is it systematically bad at?

---

## The three findings

We'll use these three from `tests/fixtures/sample_findings.json`. Each is ambiguous in a different way.

| ID | Title | Why it's tricky |
|---|---|---|
| `finding-002` | Brute Force Authentication Attempt | 47 failed logins for `admin` from one IP in 5 minutes. Sounds bad — but could be a misconfigured automation, a sysadmin who fat-fingered, or a real attack. The agent has to weigh thin evidence. |
| `finding-004` | Lateral Movement via RDP | Contractor user RDP from `workstation-089` to `fileserver-01`. Could be normal admin work, could be a compromised contractor account. Agent has no user-history context. |
| `finding-005` | Data Exfiltration Attempt | 500 MB upload to `transfer.sh` (a real anonymous file-share). Could be exfil, could be a developer moving log files. No DLP classification on the data. |

If these aren't in your instance yet:

```bash
curl -X POST http://localhost:6987/api/findings/ \
  -H 'Content-Type: application/json' \
  -d @tests/fixtures/sample_findings.json
```

---

## Steps

### 1. Your verdicts first (5 min)

Without running anything in Vigil yet, write down your own triage for each:

| Finding | Your verdict | Your confidence | One-sentence reasoning |
|---|---|---|---|
| 002 | FP / Suspicious / Confirmed | High / Med / Low | |
| 004 | FP / Suspicious / Confirmed | High / Med / Low | |
| 005 | FP / Suspicious / Confirmed | High / Med / Low | |

Don't deliberate. SOC analysts get seconds, not minutes.

### 2. Run Triage on all three (10 min)

```bash
for FID in finding-002 finding-004 finding-005; do
  echo "===== $FID ====="
  curl -s -X POST http://localhost:6987/api/claude/agent/task \
    -H 'Content-Type: application/json' \
    -d "{
      \"agent_id\": \"triage\",
      \"task\": \"Triage finding $FID. Give a severity verdict, your confidence level, and recommended next step. Explicitly call out any evidence you would want but don't have.\",
      \"max_turns\": 6
    }" | jq -r '.result'
  echo
done
```

Or run each via the UI's investigate button if you prefer reading the streaming output.

### 3. Score each agent verdict (10 min)

For each finding, fill in:

| Finding | Agent's verdict | Agent's confidence | Matches yours? | Did agent acknowledge missing data? | Confabulation? (Yes/No) |
|---|---|---|---|---|---|
| 002 | | | | | |
| 004 | | | | | |
| 005 | | | | | |

**Confabulation** = the agent stated something as fact that wasn't supported by the input data. Examples:
- Asserts the source IP is "known malicious" without evidence of an OSINT lookup.
- Names a specific threat actor without justification.
- Claims a user "frequently uses RDP" or "rarely uses RDP" without access to history.

**Did agent acknowledge missing data?** = did it explicitly say "I'd want to see user login history" or "I'd want DLP classification on the uploaded files" before making the call? An agent that asks the right questions is dramatically more useful than one that confidently answers without them.

### 4. The cross-finding test (5 min)

A subtle systematic failure mode is *findings of the same shape getting different verdicts for no good reason*. Re-run Triage on `finding-002` three times. Compare the three verdicts:

```bash
for i in 1 2 3; do
  echo "===== Run $i ====="
  curl -s -X POST http://localhost:6987/api/claude/agent/task \
    -H 'Content-Type: application/json' \
    -d '{"agent_id":"triage","task":"Triage finding finding-002.","max_turns":6}' \
    | jq -r '.result' | head -20
  echo
done
```

How stable is the verdict across runs? If it swings between "low" and "high" with the same input, that's a calibration problem.

---

## Success criteria

- [ ] You have a filled-out comparison table for all three findings (Step 3).
- [ ] You documented at least one **confabulation** the agent committed, with the specific sentence that was unsupported.
- [ ] You documented at least one case where the agent **correctly acknowledged missing data**.
- [ ] You ran `finding-002` three times and have a verdict on the agent's **calibration** — does it produce stable verdicts on stable input?
- [ ] You have one paragraph in your notes titled **"What I'd need to see before trusting this in production"** — concrete, not vague.

The last bullet is the single most valuable thing you produce in this lab series. Take 5 minutes on it.

---

## Findings to capture

1. The strongest failure mode you observed. (One sentence.)
2. The strongest success — a case where the agent's reasoning was better than yours might have been on shift. (One sentence — be honest, this matters too.)
3. Your calibration verdict: is the agent's stated confidence well-correlated with whether it's actually right?
4. What instrumentation, integration, or guardrail you'd add before letting this run autonomously.

---

## Where to look next

- The Triage agent's full prompt: [services/soc_agents.py](../../services/soc_agents.py).
- The base prompt that wraps every agent (this is where the "acknowledge uncertainty" instructions live, if they exist): same file, search for `BASE_PROMPT`.
- AI Decisions page in the UI — pull up your three Triage runs and check whether confidence calibration is logged.

→ Continue to **[Lab 05 — Audit Trail & Cost](05-audit-trail-and-cost.md)**.
