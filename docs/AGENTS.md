# SOC AI Agents

12 specialized AI agents for different security operations tasks.

## Tool Access

All agents have access to tools through two integration methods:

### Agent SDK (Recommended - Web UI)
- **19 Backend Tools** via Claude API function calling
- Security detections, case management, approvals, MITRE ATT&CK
- **Zero desktop dependency** - Works entirely through the API
- Production-ready for multi-user deployments
- Enabled by default in web UI

### MCP Tools (Optional - Advanced Use)
- **100+ Extended Tools** for specialized integrations
- Requires additional MCP server configuration
- Best for advanced local workflows or specialized integrations

> **Default Behavior**: Agents automatically use backend tools via Agent SDK in the web UI with no additional configuration needed.

## Quick Reference

| Agent | Speed | Thinking | Use When |
|-------|-------|----------|----------|
| Triage | Fast | No | Prioritizing alert queue |
| Investigator | Thorough | Yes | Deep-dive analysis |
| Threat Hunter | Balanced | Yes | Proactive threat hunting |
| Correlator | Balanced | Yes | Linking related alerts |
| Responder | Fast | No | Immediate action items |
| Reporter | Balanced | No | Documentation, reports |
| MITRE Analyst | Balanced | Yes | ATT&CK technique mapping |
| Forensics | Thorough | Yes | Artifact analysis |
| Threat Intel | Balanced | Yes | IOC enrichment |
| Compliance | Balanced | No | Policy/regulation checks |
| Malware Analyst | Thorough | Yes | Malware analysis |
| Network Analyst | Balanced | Yes | Traffic analysis |
| Auto-Responder | Balanced | Yes | Autonomous actions |

## Workflows

These are executable multi-agent playbooks defined in the [`/workflows/`](/workflows/) directory. Each workflow has a `WORKFLOW.md` file with YAML frontmatter metadata and detailed phase-by-phase instructions.

Workflows can be executed from the **Workflows** page in the UI or via the `/api/workflows/{id}/execute` API endpoint.

| Workflow | Agents | Use Case | Workflow File |
|----------|--------|----------|---------------|
| **Incident Response** | Triage -> Investigator -> Responder -> Reporter | Active incident handling | [`workflows/incident-response/WORKFLOW.md`](/workflows/incident-response/WORKFLOW.md) |
| **Full Investigation** | Investigator -> MITRE Analyst -> Correlator -> Responder -> Reporter | Deep-dive analysis with ATT&CK mapping | [`workflows/full-investigation/WORKFLOW.md`](/workflows/full-investigation/WORKFLOW.md) |
| **Threat Hunt** | Threat Hunter -> Network Analyst -> Malware Analyst -> Threat Intel -> Reporter | Proactive hypothesis-driven hunting | [`workflows/threat-hunt/WORKFLOW.md`](/workflows/threat-hunt/WORKFLOW.md) |
| **Forensic Analysis** | Forensics -> Malware Analyst -> Network Analyst -> Reporter | Post-incident forensics with chain of custody | [`workflows/forensic-analysis/WORKFLOW.md`](/workflows/forensic-analysis/WORKFLOW.md) |

### Adding Custom Workflows

Create a new directory under `workflows/` with a `WORKFLOW.md` file:

```
workflows/
  my-custom-workflow/
    WORKFLOW.md    # YAML frontmatter + markdown workflow definition
```

The backend discovers workflows automatically on startup. Use `POST /api/workflows/reload` to pick up changes without restart.

## Agent Capabilities

### Triage Agent
- Rapid alert assessment
- Severity scoring
- False positive identification
- Escalation decisions

### Investigator Agent
- Root cause analysis
- Evidence collection
- Timeline reconstruction
- Cross-source correlation

### Threat Hunter Agent
- Hypothesis-driven hunting
- Anomaly detection
- Pattern recognition
- APT discovery
- **Pattern intelligence from 7,200+ detection rules**
- **Field usage learning and correlation**
- **Detection pattern extraction**

### Correlator Agent
- Multi-stage attack detection
- Campaign identification
- Entity relationship mapping
- Attack chain reconstruction

### Responder Agent
- Containment recommendations
- NIST IR framework
- Blast radius assessment
- Remediation steps

### Reporter Agent
- Executive summaries
- Technical reports
- Audience-tailored content
- Compliance documentation

### MITRE Analyst Agent
- Technique identification
- Kill chain analysis
- TTP mapping
- Framework contextualization
- **Detection coverage analysis** (71+ new tools)
- **Gap identification and prioritization**
- **Detection template generation**
- **Tribal knowledge documentation**

### Forensics Agent
- Artifact analysis
- Chain of custody
- Multi-domain forensics
- Evidence examination

### Threat Intel Agent
- IOC enrichment
- Actor attribution
- Campaign tracking
- OSINT integration

### Compliance Agent
- NIST, ISO, PCI-DSS, HIPAA, GDPR, SOC 2
- Policy validation
- Control assessment
- Audit preparation

### Malware Analyst Agent
- Static/dynamic analysis
- Family classification
- IOC extraction
- C2 identification

### Network Analyst Agent
- Flow analysis
- Protocol examination
- Lateral movement detection
- Exfiltration identification

### Auto-Responder Agent
- Confidence-based automation
- Approval workflow integration
- Autonomous containment
- Human oversight for low-confidence

## Approval Workflow Integration

All agents can submit actions to the approval queue:

| Confidence | Behavior |
|------------|----------|
| >= 0.90 | Auto-approved, executed |
| 0.85-0.89 | Auto-approved with flag |
| 0.70-0.84 | Requires manual approval |
| < 0.70 | Monitor only |

## Backend Tools by Agent (Agent SDK)

All agents use these tools via Claude API function calling:

| Agent | Primary Backend Tools |
|-------|----------------------|
| Triage | `list_findings`, `get_finding`, `create_case` |
| Investigator | All backend tools, `nearest_neighbors`, detection search |
| Threat Hunter | `nearest_neighbors`, detection search, pattern intelligence |
| Correlator | `nearest_neighbors`, `technique_rollup`, `attack_layer` |
| Responder | `create_approval_action`, `list_approval_actions`, template generation |
| Reporter | `create_attack_layer`, case export |
| MITRE Analyst | `technique_rollup`, `get_findings_by_technique`, coverage analysis, gap identification |
| Forensics | `search_findings`, evidence tools, detection search |
| Threat Intel | Detection search, IOC analysis tools |
| Malware Analyst | Detection search, pattern analysis |
| Network Analyst | Detection search, traffic pattern tools |
| Auto-Responder | `correlate_and_create_action`, approval tools, detection validation |

### Optional MCP Tools

For advanced workflows requiring external integrations (Splunk, VirusTotal, Shodan, etc.), see the MCP configuration guide in [INTEGRATIONS.md](INTEGRATIONS.md).

### Detection Engineering Tools (Security-Detections-MCP)

All agents have access to 71+ new detection engineering tools:

| Category | Available To | Tools Count |
|----------|--------------|-------------|
| Coverage Analysis | MITRE Analyst, Investigator, Responder | 6 |
| Detection Search | All Agents | 12 |
| Pattern Intelligence | Threat Hunter, MITRE Analyst | 15 |
| Template Generation | MITRE Analyst, Responder | 8 |
| Tribal Knowledge | All Agents | 20 |
| Analytics & Reporting | MITRE Analyst, Reporter | 10 |

**Total Tools**: 71 detection engineering tools + 27 existing integrations = **98+ tools available**

See [DETECTION_ENGINEERING.md](DETECTION_ENGINEERING.md) for detailed tool descriptions and workflows.

## Usage Tips

1. **Match agent to task** - Use the specialist for your need
2. **Switch freely** - Change agents mid-conversation
3. **Start with Triage** - When unsure where to begin
4. **End with Reporter** - Document findings
5. **Use thinking agents** - For complex analysis (Investigator, Hunter, Forensics)
6. **Use fast agents** - For quick decisions (Triage, Responder)
