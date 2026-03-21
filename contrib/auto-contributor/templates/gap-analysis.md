# Gap Analysis: [Company Name] vs. Open-Source Ecosystem

**Date:** [YYYY-MM-DD]
**Registry version:** [commit hash or date of open-source-projects.yaml]
**Taxonomy version:** [commit hash or date of capability-taxonomy.yaml]

---

## Coverage Summary

| Status | Count | Percentage |
|---|---|---|
| **Covered** (full open-source equivalent) | | |
| **Partially covered** (exists with limitations) | | |
| **Gap** (no open-source equivalent) | | |
| **Complementary** (different approach, same outcome) | | |

---

## Detailed Mapping

### Covered Capabilities

These capabilities already exist in the open-source ecosystem with equivalent functionality.

| # | Vendor Capability | Category | Open-Source Equivalent | Project | Notes |
|---|---|---|---|---|---|
| | | | | | |

### Partially Covered Capabilities

These exist in open source but with meaningful limitations versus the proprietary implementation.

| # | Vendor Capability | Category | Open-Source Equivalent | Project | What's Missing |
|---|---|---|---|---|---|
| | | | | | |

### Gaps — No Open-Source Equivalent

These capabilities have no current open-source equivalent and represent contribution opportunities.

| # | Vendor Capability | Category | Priority | Suggested Vigil Enhancement | Complexity |
|---|---|---|---|---|---|
| | | | P1/P2/P3 | [Agent / Skill / MCP / Rules / Template] | S/M/L |

### Complementary Approaches

The open-source ecosystem addresses these needs through a different architectural approach.

| # | Vendor Capability | Category | Open-Source Approach | Why It's Different | Advantage |
|---|---|---|---|---|---|
| | | | | | |

---

## Gap Detail

For each gap identified above, provide enough context for Phase 3 (contribution spec generation).

### Gap [#]: [Capability Name]

**What the vendor claims:** [1-2 sentences]

**Why it matters for SOC operations:** [1-2 sentences on the real-world workflow this enables]

**Where it would live in Vigil:**
- [ ] New agent in `backend/agents/`
- [ ] New skill in `skills/`
- [ ] New MCP server in `mcp-servers/`
- [ ] Enhancement to existing agent: [which one]
- [ ] New detection rules in `data/`
- [ ] New reporting template
- [ ] Other: [describe]

**Dependencies:** [Other gaps or existing components this depends on]

**Priority rationale:** [Why P1/P2/P3]

[Repeat for each gap]

---

## Open-Source Advantages Not Covered by Vendor

Capabilities that Vigil or the open-source ecosystem provides but the proprietary vendor does not claim or disclose:

| # | Capability | Project | Why It Matters |
|---|---|---|---|
| | Full source code transparency | All | Auditability, customization, trust |
| | Foundation model flexibility | Vigil | No vendor lock-in on LLM choice |
| | Community-contributed detection rules | Vigil + Sigma | 7,200+ rules with coverage analysis |
| | Data sovereignty (runs in your environment) | All | No data leaves your infrastructure |
| | Cost transparency | All | API costs only, no enterprise licensing |
| | | | |

---

## Recommendations

**Top 3 contributions to prioritize:**

1. **[Gap name]** — [1 sentence on why this is highest priority]
2. **[Gap name]** — [1 sentence]
3. **[Gap name]** — [1 sentence]

**Suggested new Vigil Skill (if applicable):**

If the gaps cluster into a coherent workflow, recommend a new Skill that chains the required agents:

- Skill name: [e.g., "Purple Team"]
- Agent sequence: [Agent1 → Agent2 → Agent3 → ...]
- What it does: [1-2 sentences]
- Which gaps it closes: [list gap numbers]
