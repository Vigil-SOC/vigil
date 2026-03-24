# contrib/ — Community & Development Tools

This directory contains tools for **building** Vigil, not tools that **run inside** Vigil.

The `skills/` directory at the repo root contains operational SOC skills (Incident Response, Threat Hunt, etc.) that Vigil's agents execute at runtime. The tools in `contrib/` are used by contributors and maintainers to research, plan, and extend Vigil's capabilities.

## Available Tools

### auto-contributor/

A Claude skill that automates competitive research against proprietary AI security platforms and generates actionable contribution specifications for Vigil.

**What it does:**
1. Researches a given proprietary AI security company's claimed capabilities
2. Maps those capabilities against Vigil, ARTEMIS, and other open-source projects
3. Identifies gaps where no open-source equivalent exists
4. Suggests specific contributions (agents, skills, MCP servers, detection rules) to close each gap
5. Produces a comparison table and GitHub-issue-ready specifications

**How to use it:**

The auto-contributor is a Claude skill — it runs inside Claude, not inside the Vigil SOC platform. Choose the setup that matches how you use Claude:

**Claude Code (recommended):**
```bash
# From your vigil clone
cp -r contrib/auto-contributor ~/.claude/skills/
```
Then ask Claude: `"Run auto-contributor on Dropzone AI"`

**Claude.ai / claude.ai Projects:**
1. Create a Project (or go to Settings → Profile)
2. Paste the contents of `contrib/auto-contributor/SKILL.md` into the custom instructions
3. Upload `data/registry/open-source-projects.yaml` and `data/taxonomy/capability-taxonomy.yaml` to the Project
4. Ask: `"Analyze Radiant Security against Vigil"`

**Claude Desktop:**
1. Copy `contrib/auto-contributor/` into your configured skills directory
2. Ask Claude: `"Run auto-contributor on Exaforce"`

**Example prompts:**
- `"Run auto-contributor on Dropzone AI"`
- `"Analyze Prophet Security against Vigil"`
- `"What gaps does Torq HyperSOC expose in Vigil?"`
- `"Compare Conifers CognitiveSOC capabilities to open-source alternatives"`

**What you get back:**
1. Research report on the target company's claimed capabilities
2. Gap analysis mapping each capability to Vigil / open-source coverage
3. Comparison table (covered / partially covered / gap)
4. GitHub-issue-ready contribution specs with acceptance criteria

Pick a vendor. Run the tool. File the issues. Ship the code.

**Dependencies:**
- References `data/registry/open-source-projects.yaml` for the open-source project registry
- References `data/taxonomy/capability-taxonomy.yaml` for capability normalization

## Design Principle

Vigil's long-term goal is to be a **superset of all proprietary AI SOC solutions**. We get there one workflow at a time. The tools in `contrib/` systematize this process — they turn competitive research into concrete, contributable work items.

## Contributing a New Tool

If you build a development tool that helps extend Vigil (benchmarking, testing, documentation generation, etc.), add it here with its own directory and SKILL.md or README.
