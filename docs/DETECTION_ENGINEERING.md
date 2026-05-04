# Detection Engineering

Integration of security detection rules provides 7,200+ detection rules and AI-powered detection engineering capabilities to vigil.

## Overview

Detection engineering features:
- **7,200+ Detection Rules** across Sigma, Splunk ESCU, Elastic, and KQL formats
- **5 Core Tools** for coverage analysis, gap identification, and detection search
- **Agent SDK Integration** - Works via Claude API function calling with no additional setup
- **Web UI Compatible** - All detection tools accessible via browser
- **MITRE ATT&CK Integration** - Map detections to techniques

### Implementation

**Web UI (Default)**: Uses backend tools via Claude Agent SDK
- ✅ No additional setup required
- ✅ Production-ready deployment
- ✅ 5 core detection tools
- ✅ Direct access to 7,200+ rules
- ✅ Works through Claude API function calling

**MCP Server (Advanced)**: Optional for extended detection engineering workflows
- Additional tools (71+) for specialized workflows
- Tribal knowledge features
- Expert workflow prompts
- Requires MCP server configuration

## Quick Start

### Installation

Detection repositories are automatically cloned during `./setup_dev.sh`:

```bash
./setup_dev.sh
# Automatically clones ~4GB of detection repositories to ~/security-detections/
```

To skip automatic installation:
```bash
SKIP_DETECTION_REPOS=true ./setup_dev.sh
```

To update existing repositories:
```bash
./scripts/setup_detection_repos.sh --update
```

### Verify Installation

```bash
python scripts/test_detection_integration.py
```

This verifies:
- ✅ All 4 detection repositories cloned
- ✅ ~7,200+ rules indexed
- ✅ MCP server configured
- ✅ Environment variables set

## Tool Categories

### 1. Coverage Analysis (6 tools)

Quantify your detection coverage across MITRE ATT&CK techniques:

| Tool | Description | Example |
|------|-------------|---------|
| `analyze_coverage` | Check coverage for specific techniques | `analyze_coverage(techniques=["T1059.001", "T1071.001"])` |
| `identify_gaps` | Find missing detections for threat/campaign | `identify_gaps(context="ransomware")` |
| `get_coverage_stats` | Overall statistics by format | `get_coverage_stats(source_type="splunk")` |
| `get_technique_coverage` | Coverage for single technique | `get_technique_coverage("T1486")` |
| `compare_coverage` | Compare across formats | `compare_coverage(techniques=["T1059.001"])` |
| `export_coverage_report` | Generate coverage report | `export_coverage_report(format="json")` |

**Example Workflow:**
```
1. Investigate incident → Identify techniques: T1071.001, T1573.001, T1486
2. analyze_coverage(["T1071.001", "T1573.001", "T1486"])
3. Result: 85% coverage - T1486 has weak coverage
4. identify_gaps("ransomware") → Strategic view of all gaps
5. Prioritize T1486 for new detection development
```

### 2. Detection Search (12 tools)

Find relevant detection rules:

| Tool | Description | Example |
|------|-------------|---------|
| `search_detections` | Full-text search across all rules | `search_detections(query="powershell base64")` |
| `get_detection` | Get specific detection by ID | `get_detection(detection_id="det_123")` |
| `search_by_technique` | Find all detections for technique | `search_by_technique(technique="T1059.001")` |
| `search_by_tactic` | Find detections by ATT&CK tactic | `search_by_tactic(tactic="initial-access")` |
| `search_by_data_source` | Filter by data source | `search_by_data_source(source="Sysmon")` |
| `search_by_platform` | Filter by platform | `search_by_platform(platform="windows")` |
| `filter_by_severity` | Filter by severity level | `filter_by_severity(severity="high")` |
| `get_related_detections` | Find similar detections | `get_related_detections(detection_id="det_123")` |

**Example Workflow:**
```
1. Find C2 detections: search_by_technique("T1071.001")
2. Filter to Splunk: filter results by source_type="splunk"
3. Review high-confidence detections
4. get_related_detections() to discover variants
```

### 3. Pattern Intelligence (15 tools)

Learn from existing detection patterns:

| Tool | Description | Example |
|------|-------------|---------|
| `extract_patterns` | Extract patterns from technique | `extract_patterns(technique="T1059.001")` |
| `get_patterns` | Get patterns for technique/format | `get_patterns("T1059.001", format="splunk")` |
| `learn_field_usage` | Common fields for technique | `learn_field_usage(technique="T1059.001")` |
| `get_common_filters` | Common filter patterns | `get_common_filters(technique="T1071.001")` |
| `analyze_detection_logic` | Understand detection logic | `analyze_detection_logic(detection_id="det_123")` |
| `extract_iocs` | Extract IOCs from detections | `extract_iocs(technique="T1071.001")` |
| `get_field_mappings` | Cross-format field mappings | `get_field_mappings(source="sysmon")` |

**Example Workflow:**
```
1. Need to detect PowerShell execution
2. extract_patterns("T1059.001") → Learn from 50+ existing rules
3. Common patterns: CommandLine contains "powershell", "-enc", "bypass"
4. learn_field_usage("T1059.001") → Process.CommandLine, ParentImage
5. Use patterns to inform new detection
```

### 4. Template Generation (8 tools)

AI-assisted detection rule creation:

| Tool | Description | Example |
|------|-------------|---------|
| `generate_template` | Create detection template | `generate_template("T1486", format="splunk", data_source="Sysmon")` |
| `suggest_improvements` | Improve existing detection | `suggest_improvements(detection_id="det_123")` |
| `customize_template` | Adapt template to environment | `customize_template(template_id="tpl_123", params={...})` |
| `validate_detection` | Check detection syntax | `validate_detection(content="...", format="splunk")` |
| `convert_format` | Convert between formats | `convert_format(detection_id="det_123", to_format="kql")` |

**Example Workflow:**
```
1. Need Splunk detection for T1486 (Ransomware encryption)
2. generate_template("T1486", "splunk", "Sysmon")
3. Returns template with:
   - Common file extension patterns (.locked, .encrypted)
   - High file modification rates
   - Suspicious process behaviors
4. customize_template() to add environment-specific IOCs
5. validate_detection() before deployment
6. Deploy via approval workflow
```

### 5. Tribal Knowledge (20 tools)

Document and retrieve detection engineering decisions:

| Tool | Description | Example |
|------|-------------|---------|
| `log_decision` | Document analytical decision | `log_decision(context="ransomware", decision="Prioritize T1486", reasoning="...")` |
| `create_entity` | Create knowledge entity | `create_entity(name="APT29 Campaign", type="threat")` |
| `link_detection_to_entity` | Connect detection to entity | `link_detection_to_entity("det_123", "APT29", relation="detects")` |
| `get_relevant_decisions` | Retrieve past decisions | `get_relevant_decisions(context="ransomware")` |
| `tribal_knowledge_query` | Natural language query | `tribal_knowledge_query("Why did we prioritize C2 detections?")` |
| `add_learning` | Document lesson learned | `add_learning(topic="C2 detection", insight="Base64 encoding common")` |
| `get_entity_graph` | View knowledge relationships | `get_entity_graph(entity_id="ent_123")` |

**Example Workflow:**
```
1. Investigate ransomware campaign
2. create_entity("Ransomware Campaign 2026", "threat")
3. Analyze gaps → Weak T1486 coverage
4. log_decision(
     context="Ransomware gap analysis",
     decision="Prioritize T1486 detection",
     reasoning="Final stage of attack, critical for prevention"
   )
5. Generate detection → det_456
6. link_detection_to_entity("det_456", "Ransomware Campaign 2026", "prevents")
7. 6 months later: get_relevant_decisions("ransomware") 
   → Understand why T1486 was prioritized
```

### 6. Analytics & Reporting (10 tools)

Metrics and reporting:

| Tool | Description |
|------|-------------|
| `get_detection_count` | Count detections by criteria |
| `get_technique_distribution` | Distribution across techniques |
| `calculate_coverage_percentage` | Overall coverage % |
| `generate_gap_report` | Detailed gap analysis |
| `export_navigator_layer` | ATT&CK Navigator layer |
| `get_detection_timeline` | Detection creation timeline |
| `analyze_detection_quality` | Quality metrics |

## Expert Workflow Prompts

Security-Detections-MCP includes 11 guided workflow prompts:

### 1. apt-threat-emulation
Purple team exercise for APT groups
```
Usage: "Run apt-threat-emulation for APT29"
```

### 2. coverage-analysis
Comprehensive coverage assessment
```
Usage: "Run coverage-analysis for our Splunk deployment"
```

### 3. detection-tuning
Optimize existing detections
```
Usage: "Run detection-tuning for T1059.001 detections"
```

### 4. gap-prioritization
Prioritize detection gaps
```
Usage: "Run gap-prioritization for ransomware threats"
```

### 5. mitre-mapping
Map findings to ATT&CK
```
Usage: "Run mitre-mapping for incident XYZ"
```

### 6. purple-team-report
Generate purple team report
```
Usage: "Run purple-team-report for Q1 2026"
```

### 7. threat-landscape-sync
Align to threat landscape
```
Usage: "Run threat-landscape-sync for finance sector"
```

### 8. detection-validation
Validate detection effectiveness
```
Usage: "Run detection-validation for detection det_123"
```

### 9. sigma-to-platform
Convert Sigma to platform-specific
```
Usage: "Run sigma-to-platform for Splunk"
```

### 10. coverage-heatmap
Visualize coverage
```
Usage: "Run coverage-heatmap for our environment"
```

### 11. detection-lifecycle
Manage detection lifecycle
```
Usage: "Run detection-lifecycle for quarterly review"
```

## Agent Integration

### MITRE Analyst Agent (Primary User)

The MITRE Analyst agent is the primary consumer of detection engineering tools:

**Common Workflows:**
1. **Coverage Analysis** - "What's our detection coverage for APT29?"
2. **Gap Identification** - "What detection gaps exist for ransomware?"
3. **Template Generation** - "Generate a Splunk detection for T1059.001"
4. **Decision Documentation** - Auto-logs why techniques were prioritized

**Updated Tools:**
- All coverage analysis tools
- Template generation tools
- Tribal knowledge documentation
- Gap identification tools

### Threat Hunter Agent

Uses pattern intelligence for proactive hunting:

**Common Workflows:**
1. **Pattern Learning** - Extract patterns from successful detections
2. **Field Intelligence** - Understand which fields detect specific behaviors
3. **Historical Context** - Review past hunting decisions
4. **Hypothesis Generation** - Use patterns to generate hunting hypotheses

**Updated Tools:**
- Pattern extraction tools
- Field usage learning
- Tribal knowledge queries
- Detection search tools

### Investigator Agent

Validates detection coverage during investigations:

**Common Workflows:**
1. **Coverage Validation** - Would similar attacks be detected?
2. **Detection Review** - What detections triggered for this incident?
3. **Gap Documentation** - Document detection gaps discovered
4. **Context Retrieval** - Understand past investigation decisions

**Updated Tools:**
- Coverage analysis
- Detection search
- Tribal knowledge queries
- Historical decision retrieval

### Responder Agent

Recommends new detections to prevent recurrence:

**Common Workflows:**
1. **Prevention Recommendations** - Generate detections for containment
2. **Template Creation** - Create detection templates for approval
3. **Coverage Validation** - Ensure incident would be detected
4. **Decision Logging** - Document response detection decisions

**Updated Tools:**
- Template generation
- Coverage validation
- Approval integration
- Decision logging

### All Agents

All 13 agents can:
- Search detection rules
- Query tribal knowledge
- Validate coverage
- Document decisions

## Integration with Existing Features

### Case Management Integration

Link detections to cases:
```
1. Investigate case → Identify weak coverage
2. Generate detection template
3. Add to case notes with link_detection_to_entity()
4. Submit for approval via approval workflow
5. Deploy to SIEM upon approval
```

### Approval Workflow Integration

New detections flow through approval:
```
1. Generate template → det_456
2. Create approval action:
   - Action: Deploy detection det_456
   - Confidence: 0.85 (requires approval)
   - Reasoning: Addresses T1486 gap
3. Analyst reviews and approves
4. Auto-deploy to Splunk/Elastic
```

### MITRE ATT&CK Integration

Works with existing attack layer tools:
```
1. Generate coverage layer with get_attack_layer()
2. analyze_coverage() for weak techniques
3. Update layer with new detections
4. Visualize in ATT&CK Navigator
```

## Configuration

### Detection Repository Paths

Configured in `.env`:
```bash
SIGMA_PATHS="${HOME}/security-detections/sigma/rules"
SPLUNK_PATHS="${HOME}/security-detections/security_content/detections"
ELASTIC_PATHS="${HOME}/security-detections/detection-rules/rules"
KQL_PATHS="${HOME}/security-detections/Hunting-Queries-Detection-Rules"
STORY_PATHS="${HOME}/security-detections/security_content/stories"
```

### MCP Server Configuration

In `mcp-config.json`:
```json
{
  "security-detections": {
    "command": "npx",
    "args": ["-y", "security-detections-mcp"],
    "env": {
      "SIGMA_PATHS": "${HOME}/security-detections/sigma/rules",
      "SPLUNK_PATHS": "${HOME}/security-detections/security_content/detections",
      "ELASTIC_PATHS": "${HOME}/security-detections/detection-rules/rules",
      "KQL_PATHS": "${HOME}/security-detections/Hunting-Queries-Detection-Rules",
      "STORY_PATHS": "${HOME}/security-detections/security_content/stories"
    }
  }
}
```

### Updating Detection Repositories

```bash
# Update all repositories
./scripts/setup_detection_repos.sh --update

# View repository status
ls -lh ~/security-detections/
```

## Example Workflows

### Workflow 1: Ransomware Coverage Assessment

```
You: "Switch to MITRE Analyst agent"
Claude: [Switches to MITRE Analyst]

You: "Analyze our detection coverage for ransomware attacks"
Claude: [Uses identify_gaps("ransomware")]
        "Gap analysis shows weak coverage for:
         - T1486 (Data Encrypted for Impact): 2 detections
         - T1490 (Inhibit System Recovery): 1 detection
         - T1489 (Service Stop): 0 detections
         
         Recommendation: Prioritize T1486 and T1490"

You: "Generate a Splunk detection for T1486"
Claude: [Uses generate_template("T1486", "splunk", "Sysmon")]
        "Here's a template detecting rapid file encryption..."

You: "Add this to case-2026-ransomware"
Claude: [Creates approval action, links to case]
        "Detection submitted for approval and linked to case"
```

### Workflow 2: APT Threat Emulation

```
You: "Run apt-threat-emulation for APT29"
Claude: [Uses expert prompt + mitre-attack-mcp]
        "APT29 Analysis:
         
         Techniques Used: 15
         - T1566.001 (Spearphishing Attachment)
         - T1059.001 (PowerShell)
         - T1071.001 (Web Protocols)
         ...
         
         Coverage: 67% (10/15 techniques)
         
         Gaps:
         - T1027.005 (Indicator Removal)
         - T1552.001 (Credentials in Files)
         ...
         
         Purple Team Plan:
         1. Test T1566.001 with sample phish
         2. Execute PowerShell techniques
         3. Validate C2 detections
         ..."
```

### Workflow 3: Detection Tuning

```
You: "Switch to Threat Hunter agent"
Claude: [Switches to Threat Hunter]

You: "What patterns exist for detecting C2 beaconing?"
Claude: [Uses extract_patterns("T1071.001")]
        "Analyzed 127 C2 detection rules. Common patterns:
         
         1. Regular beaconing intervals (60s, 300s, 3600s)
         2. Small payload sizes (< 1KB)
         3. HTTPS to non-standard ports
         4. User-Agent anomalies
         5. Base64-encoded payloads
         
         Field mappings:
         - Splunk: dest_port, http_method, bytes_out
         - Elastic: destination.port, http.request.method
         - KQL: DestinationPort, RequestMethod"

You: "Create a hunt hypothesis using these patterns"
Claude: "Hunt for hosts with:
         - Outbound HTTPS every ~300 seconds
         - Consistent payload size < 500 bytes
         - Non-browser User-Agents
         - To suspicious ASNs"
```

## Troubleshooting

### Detection Repositories Not Found

```bash
# Check if repositories exist
ls -l ~/security-detections/

# Re-clone repositories
rm -rf ~/security-detections/
./scripts/setup_detection_repos.sh
```

### MCP Server Not Loading

```bash
# Verify configuration
cat mcp-config.json | grep security-detections

# Test MCP server standalone
npx security-detections-mcp

# Check environment variables
echo $SIGMA_PATHS
```

### Low Detection Count

```bash
# Verify repository contents
find ~/security-detections/sigma/rules -name "*.yml" | wc -l
find ~/security-detections/security_content/detections -name "*.yml" | wc -l

# Should show thousands of files
```

### Tools Not Loading

1. Restart the backend server (`uvicorn backend.main:app --reload`)
2. Check MCP server status via API: `GET /api/mcp/servers/status`
3. Verify configuration with test script:
   ```bash
   python scripts/test_detection_integration.py
   ```

## Best Practices

### 1. Document Decisions

Always log why you prioritized certain detections:
```python
log_decision(
    context="Incident response for case-123",
    decision="Prioritized T1486 detection",
    reasoning="Identified as gap during ransomware investigation",
    tags=["ransomware", "priority", "incident-driven"]
)
```

### 2. Use Pattern Intelligence

Don't start from scratch - learn from existing rules:
```python
# Before creating detection
patterns = extract_patterns("T1059.001")
fields = learn_field_usage("T1059.001")

# Use patterns to inform your detection
```

### 3. Validate Before Deploying

Always validate generated templates:
```python
# Generate template
template = generate_template("T1486", "splunk")

# Validate syntax
result = validate_detection(template, format="splunk")

# Submit for approval
create_approval_action(
    action="deploy_detection",
    confidence=0.85,
    reasoning="Validated template for T1486 gap"
)
```

### 4. Regular Coverage Reviews

Schedule quarterly reviews:
```python
# Q1 2026 Review
coverage = get_coverage_stats()
gaps = identify_gaps(context="Q1-2026-priorities")
report = generate_gap_report(format="pdf")

# Document review
log_decision(
    context="Q1 2026 Coverage Review",
    decision="Prioritize initial access techniques",
    reasoning="Coverage analysis shows 45% gap in initial access"
)
```

### 5. Cross-Reference with Incidents

Link detections to actual incidents:
```python
# After incident investigation
create_entity("Incident-2026-001", "incident")
link_detection_to_entity("det_456", "Incident-2026-001", "would_detect")

# Future benefit: "What detections cover similar incidents?"
```

## Performance Considerations

### Repository Size

Detection repositories total ~4GB:
- Sigma: ~1.5GB
- Splunk ESCU: ~1GB
- Elastic: ~800MB
- KQL: ~700MB

Use `--depth 1` for shallow clones (done automatically).

### Indexing Time

First load indexes 7,200+ rules:
- Initial index: 30-60 seconds
- Subsequent queries: < 1 second
- Pattern extraction: 2-5 seconds per technique

### Disk Usage

Monitor disk usage:
```bash
du -sh ~/security-detections/
```

Clean old checkouts if needed:
```bash
cd ~/security-detections/
git gc --aggressive --prune=all
```

## Resources

- **Security-Detections-MCP**: https://github.com/MHaggis/Security-Detections-MCP
- **Sigma Rules**: https://github.com/SigmaHQ/sigma
- **Splunk ESCU**: https://github.com/splunk/security_content
- **Elastic Rules**: https://github.com/elastic/detection-rules
- **KQL Queries**: https://github.com/Bert-JanP/Hunting-Queries-Detection-Rules
- **MITRE ATT&CK**: https://attack.mitre.org/

## Contributing

Found a detection gap or pattern? Contribute back:

1. Document in tribal knowledge system
2. Generate template with improvements
3. Submit PR to upstream repositories:
   - Sigma: https://github.com/SigmaHQ/sigma
   - Splunk: https://github.com/splunk/security_content
   - Elastic: https://github.com/elastic/detection-rules

## License

Security-Detections-MCP is Apache 2.0 licensed.
Detection rule repositories have their own licenses - see individual repos.

