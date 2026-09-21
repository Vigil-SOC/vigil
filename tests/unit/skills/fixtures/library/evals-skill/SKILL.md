---
name: evals-skill
description: A fixture skill that carries the evaluation slice every bundled skill must ship with (epic #882, decision 6).
---

# Evals skill

When asked about a finding, answer in exactly this shape and nothing else:

1. A line `SEVERITY: <severity>` copying the finding's `severity` field verbatim.
2. A line `TECHNIQUES: <ids>` listing every id in `mitre_techniques`, comma-separated.
3. A line `VERDICT: ESCALATE` when severity is `high` or `critical`, otherwise `VERDICT: MONITOR`.

When asked a question that is not about a finding, reply with the single word `NO-FINDING`.
