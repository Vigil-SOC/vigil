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
- As a Claude skill in Claude Desktop or Claude.ai (copy `auto-contributor/` to your Claude skills directory)
- As a standalone reference when planning Vigil enhancements

**Dependencies:**
- References `data/registry/open-source-projects.yaml` for the open-source project registry
- References `data/taxonomy/capability-taxonomy.yaml` for capability normalization

## Design Principle

Vigil's long-term goal is to be a **superset of all proprietary AI SOC solutions**. We get there one workflow at a time. The tools in `contrib/` systematize this process — they turn competitive research into concrete, contributable work items.

## Contributing a New Tool

If you build a development tool that helps extend Vigil (benchmarking, testing, documentation generation, etc.), add it here with its own directory and SKILL.md or README.
