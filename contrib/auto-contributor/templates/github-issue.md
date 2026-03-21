# GitHub Issue: [Enhancement Title]

---

**Labels:** `enhancement`, `[capability-category]`, `[complexity: S/M/L]`, `[priority: P1/P2/P3]`
**Milestone:** [optional — e.g., "v0.2 - Purple Team"]

---

## Problem

[2-3 sentences describing the gap. Reference the proprietary capability that exposed this gap, but do NOT name the specific vendor unless the issue is public. Frame it as a workflow need, not as "Company X has this and we don't."]

**Example:**
> Proprietary AI red-teaming platforms can reconstruct validated kill chains from offensive tool output and prioritize remediation by how many attack paths a single fix would eliminate. Vigil's Responder agent currently recommends containment actions but does not weight them by kill-chain-elimination impact. This means SOC teams using Vigil alongside offensive tools (like ARTEMIS) must manually assess which remediations have the highest blast-radius reduction.

## Proposed Solution

[Describe the solution with enough specificity that a contributor can start implementation. Include:]

### Where it lives
- [ ] New agent: `backend/agents/[agent_name].py`
- [ ] New skill: `skills/[skill-name]/SKILL.md`
- [ ] New MCP server: `mcp-servers/[server-name]/`
- [ ] Enhancement to: `[existing file path]`
- [ ] Detection rules: `data/[rules location]`
- [ ] Other: [describe]

### How it works
[3-5 bullet points describing the implementation approach. Be specific about inputs, outputs, and how this connects to existing Vigil components.]

### Interfaces
- **Input:** [What data this component consumes — e.g., ARTEMIS findings JSON, Vigil findings database]
- **Output:** [What it produces — e.g., prioritized remediation list, MITRE ATT&CK layer, Sigma rules]
- **Connects to:** [Which existing agents, skills, or MCP servers it interacts with]

## Acceptance Criteria

- [ ] [Specific, testable criterion — e.g., "Given an ARTEMIS session output with 3 kill chains, the agent produces a remediation ranking that correctly identifies the fix eliminating the most chains"]
- [ ] [Another criterion]
- [ ] [Documentation updated in docs/]
- [ ] [Tests added in tests/]
- [ ] [Works with existing Skills (specify which)]

## Dependencies

- [ ] Depends on: [other issue numbers or components that must exist first]
- [ ] Blocked by: [anything that prevents starting]
- [ ] Enables: [other issues or capabilities this unlocks]

## Complexity Estimate

**Size:** S / M / L

**Rationale:** [1-2 sentences — e.g., "M — requires a new agent with custom scoring logic, integration with the existing Responder agent, and test data generation. No new MCP servers or external dependencies."]

## Competitive Context

**Exposed by:** [General description of the proprietary capability — e.g., "Continuous AI red-teaming platform with kill-chain-prioritized remediation"]
**Category:** [From capability-taxonomy.yaml]
**Open-source coverage before this issue:** [Covered / Partially covered / Gap]
**Open-source coverage after this issue:** [Expected status]

## References

- [Link to relevant Vigil docs]
- [Link to relevant ARTEMIS docs]
- [Link to relevant research papers or blog posts]
- [Link to the auto-contributor report that generated this issue]
