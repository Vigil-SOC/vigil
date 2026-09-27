---
name: full-skill
description: A skill using every optional field the Agent Skills spec names, with a references file.
license: Apache-2.0
compatibility: Needs read access to findings; no network egress.
metadata:
  author: vigil
  version: "1.0"
allowed-tools: get_finding list_findings
---

# Full skill

## Procedure

1. Read `references/checklist.md` with `read_skill("full-skill", "references/checklist.md")`.
2. Work through the checklist against the finding.
