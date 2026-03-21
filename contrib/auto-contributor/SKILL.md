---
name: auto-contributor
description: "Competitive research and contribution planning for Vigil. Use when analyzing a proprietary AI security company to identify capability gaps versus open-source alternatives (Vigil, ARTEMIS, and others), then generating actionable contribution specifications. Triggers: 'analyze [company]', 'compare [company] to Vigil', 'what gaps does [company] expose', 'suggest Vigil contributions for [capability]', 'run auto-contributor on [URL]'. Produces research reports, gap analyses, comparison tables, and GitHub issue specs."
---

# Auto-Contributor Skill

Systematically research proprietary AI security platforms, identify capability gaps versus the open-source ecosystem, and generate contribution specifications to close those gaps — making Vigil a superset of all proprietary AI SOC solutions, one workflow at a time.

## Overview

This skill operates in a structured pipeline. Each phase produces a discrete output that feeds the next. The full pipeline can run end-to-end in a single session, or phases can be run independently.

## Pipeline Phases

### Phase 1: Research Target Company

**Input:** Company URL or name
**Output:** Structured research report (using `templates/research-report.md`)

**Steps:**
1. Fetch the company's website (homepage, platform page, product page)
2. Search for press coverage, funding announcements, analyst reports
3. Search for technical blog posts, documentation, or architecture disclosures
4. Extract and categorize every claimed capability
5. Note what is NOT disclosed (pricing, benchmarks, integrations, architecture)
6. Identify the company's primary domain focus using the capability taxonomy

**Research queries to run:**
```
web_search: "[company name] cybersecurity platform"
web_search: "[company name] capabilities features"
web_search: "[company name] funding announcement"
web_search: "[company name] architecture technical blog"
web_fetch: [company homepage URL]
web_fetch: [company platform/product URL]
```

**Extraction checklist — for each claimed capability, capture:**
- Capability name (as the vendor describes it)
- Normalized category (from `data/taxonomy/capability-taxonomy.yaml`)
- What specifically is claimed (feature, not marketing language)
- Evidence level: Website claim only / Press confirmed / Independently validated / Customer testimonial
- Integration details (if any SIEM, EDR, cloud, ticketing integrations are mentioned)

### Phase 2: Map Against Open-Source Ecosystem

**Input:** Phase 1 output (extracted capabilities)
**Output:** Gap analysis (using `templates/gap-analysis.md`)

**Steps:**
1. Load the open-source project registry from `data/registry/open-source-projects.yaml`
2. For each extracted capability, check whether it exists in:
   - **Vigil** (primary — check agents, skills, MCP servers, detection rules)
   - **ARTEMIS** (offensive capabilities)
   - **Other registered open-source projects** (Wazuh, TheHive, MISP, Caldera, etc.)
3. Classify each capability as:
   - **Covered** — exists in open source with equivalent functionality
   - **Partially covered** — exists but with meaningful limitations
   - **Gap** — no open-source equivalent
   - **Complementary** — the open-source approach is architecturally different but achieves the same outcome
4. For gaps and partial coverage, identify specifically what's missing

**Vigil capability check — where to look:**
- Agents: `docs/AGENTS.md` — 12 specialized agents and their capabilities
- Skills: `skills/` directory — 4 multi-agent workflows
- MCP integrations: `docs/INTEGRATIONS.md` — 30+ tool integrations
- Detection rules: `docs/DETECTION_ENGINEERING.md` — 7,200+ rules
- Case management: `docs/CHAT_CASE_MANAGEMENT.md`
- Architecture: `docs/README.md`

**ARTEMIS capability check:**
- Supervisor architecture (long-horizon autonomous operation)
- Sub-agent spawning (parallel exploitation)
- Automatic triaging (false-positive filtering)
- Session persistence (summarize, clear, resume)

**When checking other open-source projects:**
- Use web_search to verify current status (some projects go dormant)
- Check last commit date and release recency
- Note if the project provides an API, MCP server, or integration path to Vigil

### Phase 3: Generate Contribution Specifications

**Input:** Phase 2 output (gaps identified)
**Output:** One GitHub issue spec per gap (using `templates/github-issue.md`)

**Steps:**
1. For each gap or partial-coverage item, determine where it would live in Vigil:
   - **New Agent** — if the gap requires a new reasoning capability (e.g., Attack Path Analyzer)
   - **New Skill** — if the gap requires a new multi-agent workflow (e.g., Purple Team)
   - **New MCP Server** — if the gap requires a new tool integration
   - **Agent Enhancement** — if an existing agent needs expanded capability
   - **Detection Rules** — if the gap is coverage of specific techniques
   - **Reporting Template** — if the gap is output format (e.g., board-grade brief)
2. Estimate complexity:
   - **S** (Small) — can be contributed in a single PR; <200 lines; no new dependencies
   - **M** (Medium) — 1-3 PRs; new agent or MCP server; moderate testing needed
   - **L** (Large) — multi-PR effort; new skill with multiple agents; significant architecture work
3. Write the GitHub issue spec with:
   - Clear problem statement tied to the competitive gap
   - Proposed solution with enough detail that a contributor can start
   - Acceptance criteria (what "done" looks like)
   - Dependencies on other issues (if any)
   - Labels: `enhancement`, `good-first-issue` (for S), `help-wanted`, and the capability category

**Priority scoring:**
- **P1 (High):** Gap is a core differentiator for the proprietary platform AND addresses a real SOC workflow need
- **P2 (Medium):** Gap exists but workarounds are available, OR the gap is in a niche capability
- **P3 (Low):** Gap is cosmetic, marketing-level, or addresses a workflow that's out of scope for an AI SOC

### Phase 4: Produce Comparison Table

**Input:** All previous phase outputs
**Output:** Side-by-side comparison (using `templates/comparison-table.md`)

**Steps:**
1. Build the comparison table with columns: Dimension | Proprietary Platform | Open Source (Vigil + ARTEMIS + others)
2. Include these standard rows:
   - Each major capability category from the taxonomy
   - Cost model
   - Transparency / auditability
   - Customization / extensibility
   - Integration ecosystem
   - Data sovereignty
   - Time to value
   - Community / support model
3. Be factual. Where the proprietary platform is stronger, say so. Where open source is stronger, say so. Where they're equivalent, say so.
4. Do NOT claim open-source equivalence where it doesn't exist — the gaps are the contribution opportunities.

### Phase 5: Compile Final Report

**Output:** Single markdown document combining all phases

**Structure:**
```
# [Company Name] vs. Open Source: Research & Contribution Plan

## Executive Summary
- 3-4 sentences: what the company does, how many gaps vs. open source, top priority contributions

## Part 1: Company Research
[Phase 1 output]

## Part 2: Capability Mapping
[Phase 2 output — table format]

## Part 3: Gap Analysis & Contribution Specs
[Phase 3 output — one section per gap with linked GitHub issue spec]

## Part 4: Comparison Table
[Phase 4 output]

## Part 5: Blog Draft (Optional)
[If requested — a publishable blog post following the Vigil blog style]

## Appendix: Methodology
- Date of research
- Sources consulted
- Registry version used
- Taxonomy version used
```

## Usage Examples

### Full Pipeline
```
User: "Analyze Armadin and suggest Vigil contributions"

Auto-Contributor:
  → Phase 1: Research armadin.com, press coverage, funding
  → Phase 2: Map 11 capabilities against Vigil + ARTEMIS + registry
  → Phase 3: Generate 6 GitHub issue specs for identified gaps
  → Phase 4: Build 14-row comparison table
  → Phase 5: Compile into single report
```

### Single Phase
```
User: "Just do competitive research on Dropzone AI — no contribution specs yet"

Auto-Contributor:
  → Phase 1 only: Research report with extracted capabilities
```

### Gap Check
```
User: "Does Vigil already cover automated playbook generation?"

Auto-Contributor:
  → Phase 2 partial: Check Vigil skills, agents, and MCP servers for playbook generation
  → Return: coverage status + details
```

### Blog Generation
```
User: "Write a blog comparing our open-source approach to [company]'s capabilities"

Auto-Contributor:
  → Run full pipeline
  → Phase 5 includes blog draft: how-to guide + comparison table + Vigil positioning
```

## Key Rules

1. **Never name the proprietary company in blog output** unless explicitly told to. Use descriptive phrases: "a recently funded AI red-teaming platform," "a proprietary AI SOC vendor."
2. **Be factual in comparisons.** No hype, no FUD. If the proprietary platform is stronger somewhere, say so — that's the contribution opportunity.
3. **Always check the registry before claiming a gap.** The open-source ecosystem is broad. A capability might exist in a project you haven't considered.
4. **Prioritize contributions that create closed-loop workflows.** A new agent that doesn't connect to existing skills is less valuable than one that extends an existing skill.
5. **Update the registry** when you discover a new relevant open-source project during research. Add it to `data/registry/open-source-projects.yaml`.
6. **Update the taxonomy** when you encounter a capability category not currently covered. Add it to `data/taxonomy/capability-taxonomy.yaml`.

## Dependencies

- **Web search** — for company research and open-source project status checks
- **Web fetch** — for reading company websites and GitHub READMEs
- **File creation** — for producing the final report and GitHub issue specs
- **Registry file** — `data/registry/open-source-projects.yaml`
- **Taxonomy file** — `data/taxonomy/capability-taxonomy.yaml`

## Files Reference

```
contrib/auto-contributor/
├── SKILL.md                          # This file
└── templates/
    ├── research-report.md            # Phase 1 output template
    ├── gap-analysis.md               # Phase 2 output template
    ├── comparison-table.md           # Phase 4 output template
    └── github-issue.md              # Phase 3 output template (per gap)
```
