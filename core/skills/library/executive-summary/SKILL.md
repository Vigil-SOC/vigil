---
name: executive-summary
description: Write a security report for a chosen audience - technical report, executive summary, or one-page board brief with RED/YELLOW/GREEN risk posture, key metrics, top 3 actions and 30/60/90 day trend. Use when asked for a report, summary, board brief, board report or risk posture report over cases and findings.
metadata:
  version: "1"
---

# Executive summary

1. Gather data via tools (cases, findings, actions)
2. Analyze context: severity, timeline, impact
3. Determine report type from user request:

   TECHNICAL REPORT (default):
   - Executive Summary: Business impact, plain language
   - Technical Details: Evidence for security team
   - Timeline: Chronological events
   - Actions Taken: Response measures
   - Recommendations: Next steps

   EXECUTIVE SUMMARY:
   - Tailor to executive audience, minimize technical jargon

   BOARD BRIEF (triggered by "board brief", "board report", "risk posture report"):
   - Follow the board-brief template: `read_skill("executive-summary", "assets/board-brief.md")`
   - Structure: Risk Posture → Key Metrics → Top 3 Actions → Trend
   - Risk Posture: RED (active breach or uncontained critical threats),
     YELLOW (open critical findings with remediation in progress),
     GREEN (no open criticals, remediation on track)
   - Key Metrics (pull from actual data, never hallucinate):
     * Validated kill chains or critical finding chains (current vs prior period)
     * Detection coverage percentage (findings with case coverage)
     * Mean time to remediation (from case open to resolved)
     * Open critical findings count
   - Top 3 Action Items: Each with risk (one sentence), fix type
     (budget/policy/technical), estimated impact if addressed
   - 30/60/90 Day Trend: Exposure count direction (improving/stable/degrading)
   - Language: Non-technical throughout. No CVE numbers, no ATT&CK IDs
     in the main body. Use plain business language.
   - Length: One page equivalent. Brevity is mandatory.
   - Output: Markdown for chat, note PDF export is available

4. Tailor to audience: Board/CEO vs Executive vs Technical vs Compliance
