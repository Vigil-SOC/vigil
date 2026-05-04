# Chat-Driven Case Management

## Overview

The Vigil chat interface now has powerful capabilities to automatically build out and manage cases based on natural language prompts. When you're examining findings and identify patterns that are part of a kill chain, you can simply tell the chat to add them to a case, and it will handle all the details automatically.

## Core Capabilities

### 1. Automatic Case Building

When investigating, Claude can automatically:
- Add findings to cases
- Log activities and notes
- Build kill chain timelines
- Tag MITRE ATT&CK techniques
- Document resolution steps
- Track all investigation actions

### 2. Natural Language Commands

You don't need to use specific syntax. Just describe what you want in natural language:

```
✓ "Add this to case-20260121-abc123"
✓ "This needs to go into case XYZ"
✓ "Create a case for this lateral movement campaign"
✓ "Note that this looks like T1021 lateral movement"
✓ "Log this as part of the kill chain"
✓ "Add these three findings to the APT case"
```

## Example Workflows

### Workflow 1: Building a Case During Investigation

```
You: "Show me high severity findings from the last 24 hours"

Claude: [Lists findings f-001, f-002, f-003...]

You: "f-001 looks like initial access. Analyze it."

Claude: [Analyzes finding, determines it's T1078 - Valid Accounts]

You: "Create a case called 'APT Lateral Movement Campaign' and add this finding"

Claude: ✓ Created case case-20260121-abc123
        ✓ Added f-001 to case
        ✓ Tagged with T1078
        ✓ Logged activity: "Initial Access via compromised credentials"

You: "f-002 and f-003 look related - lateral movement via RDP. Add them to this case."

Claude: ✓ Added f-002 and f-003 to case-20260121-abc123
        ✓ Tagged with T1021.001 (RDP)
        ✓ Updated timeline with lateral movement phase
        ✓ Logged activity: "Lateral movement detected across 3 hosts"

You: "Note that the attacker moved from workstation-42 to server-15"

Claude: ✓ Added activity to case
        ✓ Added timeline entry documenting the lateral movement path
```

### Workflow 2: Creating a Case from Kill Chain Analysis

```
You: "I've identified an attack campaign. Initial access via phishing (f-101), 
      lateral movement through RDP (f-102, f-103), and data exfil to external IP (f-104).
      Create a case documenting this kill chain."

Claude: ✓ Created case case-20260121-xyz789 "Phishing to Exfiltration Campaign"
        ✓ Added 4 findings organized by kill chain stage
        ✓ Timeline created with 3 stages:
          - Initial Access (T1566 - Phishing)
          - Lateral Movement (T1021.001 - RDP)
          - Exfiltration (T1048.003 - Exfil Over Alternative Protocol)
        ✓ All MITRE techniques tagged
        ✓ Marked as HIGH priority
```

### Workflow 3: Ongoing Case Updates

```
You: "Add a note to case-20260121-abc123 that we've isolated the affected hosts"

Claude: ✓ Added activity to case-20260121-abc123
        ✓ Type: action_taken
        ✓ Description: "Isolated affected hosts from network"
        ✓ Logged at: 2026-01-21T15:30:00Z

You: "Add a resolution step - we blocked the C2 domain at the firewall"

Claude: ✓ Added resolution step to case-20260121-abc123
        ✓ Action: Blocked C2 domain evil.com at perimeter firewall
        ✓ Result: All outbound connections to C2 blocked
```

## Available Tools

Claude has access to these comprehensive MCP tools for case management:

### Finding Management
- `add_finding_to_case(case_id, finding_id)` - Add a single finding
- `bulk_add_findings_to_case(case_id, finding_ids, note)` - Add multiple findings at once
- `remove_finding_from_case(case_id, finding_id)` - Remove a finding

### Case Creation & Updates
- `create_case(title, finding_ids, description, priority, status, assignee, tags)` - Create new case
- `update_case(case_id, title, description, status, priority, assignee, add_note)` - Update case details
- `create_case_from_killchain(title, finding_ids, killchain_stages, ...)` - Create structured kill chain case

### Activity Logging
- `add_case_activity(case_id, activity_type, description, details)` - Log investigation activities
  - Activity types: `note`, `status_change`, `finding_added`, `action_taken`, `investigation_step`, `analysis`, `communication`, `task_update`

### Timeline Management
- `add_case_timeline_entry(case_id, event_description, event_time, event_type, details)` - Add chronological events
  - Event types: `attack`, `detection`, `investigation`, `response`

### MITRE ATT&CK Mapping
- `add_case_mitre_techniques(case_id, technique_ids)` - Tag MITRE techniques to document TTPs

### Resolution Tracking
- `add_resolution_step(case_id, description, action_taken, result)` - Document remediation actions

### Comments & Collaboration 🆕
- `add_case_comment(case_id, author, content, parent_comment_id)` - Add comment (supports threading)
- `get_case_comments(case_id)` - Get all comments

### Evidence Management 🆕
- `add_case_evidence(case_id, evidence_type, name, collected_by, description, file_path, source, tags)` - Add evidence with chain of custody
  - Evidence types: `file`, `log`, `network_capture`, `memory_dump`, `screenshot`

### IOC Management 🆕
- `add_case_ioc(case_id, ioc_type, value, threat_level, confidence, source, tags, context)` - Add single IOC
  - IOC types: `ip`, `domain`, `hash`, `url`, `email`, `file_name`
- `bulk_add_iocs(case_id, iocs)` - Add multiple IOCs at once
- `get_case_iocs(case_id, ioc_type)` - Get all IOCs

### Task Management 🆕
- `add_case_task(case_id, title, description, assignee, priority, due_date)` - Create investigation task
- `update_case_task(task_id, status, assignee, notes)` - Update task status
  - Task statuses: `pending`, `in_progress`, `completed`, `cancelled`
- `get_case_tasks(case_id)` - Get all tasks

### Case Relationships 🆕
- `link_related_cases(case_id, related_case_id, relationship_type, created_by, notes)` - Link cases together
  - Relationship types: `duplicate`, `related`, `parent`, `child`, `blocks`, `blocked_by`

### Escalations 🆕
- `escalate_case(case_id, escalated_from, escalated_to, reason, urgency_level)` - Escalate case
  - Urgency levels: `low`, `medium`, `high`, `critical`

### Case Closure 🆕
- `close_case(case_id, closure_category, closed_by, root_cause, lessons_learned, recommendations, executive_summary)` - Close case with full metadata
  - Closure categories: `resolved`, `false_positive`, `duplicate`, `unable_to_resolve`

### Case Retrieval
- `list_cases(status, priority, limit)` - List all cases
- `get_case(case_id)` - Get full case details with findings

## Best Practices

### 1. Be Descriptive
When adding things to cases, provide context:
```
❌ "Add this to the case"
✓ "Add this finding to case-123 - it shows the attacker's persistence mechanism"
```

### 2. Build Timeline as You Investigate
Document events chronologically as you discover them:
```
✓ "Add a timeline entry - malware first seen at 2026-01-21 10:00 UTC"
✓ "Timeline entry: attacker moved laterally at 10:15 UTC"
✓ "Timeline entry: data exfiltration detected at 10:30 UTC"
```

### 3. Tag MITRE Techniques
Help build the full kill chain picture:
```
✓ "This is T1071.001 - tag it in the case"
✓ "Add T1059.001 and T1003.001 to the case - PowerShell and credential dumping"
```

### 4. Log Your Actions
Document what you've done:
```
✓ "Note that I've contacted the user whose account was compromised"
✓ "Log that I've isolated the affected systems"
✓ "Add a resolution step - installed security patches on all affected hosts"
```

### 5. Use Bulk Operations
When you identify multiple related findings:
```
✓ "Add findings f-101, f-102, f-103, f-104, and f-105 to this case - all show the same attack pattern"
```

## Integration with Investigation Workflow

The chat-driven case management integrates seamlessly with your investigation:

1. **Start Investigation**: Use chat to query findings, analyze patterns
2. **Identify Patterns**: Claude helps correlate findings and identify kill chains
3. **Build Case**: As you investigate, tell Claude to add findings to cases
4. **Document Analysis**: Your notes and observations are logged automatically
5. **Track Actions**: Resolution steps are documented as you take them
6. **Close Case**: Case has full timeline, findings, and resolution documented

## Example: Full Investigation Session

```
You: "Show me findings with lateral movement indicators"

Claude: [Shows 5 findings with T1021.* techniques]

You: "Get details on f-20260121-001"

Claude: [Shows RDP login from unusual source]

You: "This looks like the start of an attack. Create a case called 'Lateral Movement Investigation'"

Claude: ✓ Created case case-20260121-def456
        ✓ Added f-20260121-001
        ✓ Tagged with T1021.001

You: "Find similar findings using nearest neighbors"

Claude: [Shows 4 similar findings with RDP patterns]

You: "Add all of these to the case - they're part of the same campaign"

Claude: ✓ Added 4 findings to case-20260121-def456
        ✓ Updated activity log
        ✓ Added timeline entries for each event

You: "Analyze the source IPs - are they malicious?"

Claude: [Uses threat intel tools to analyze IPs]
        ✓ IP 192.168.50.10 appears compromised
        ✓ IP 192.168.50.15 shows suspicious activity
        ✓ Logged analysis in case activities

You: "I've isolated those two systems. Log that as a resolution step."

Claude: ✓ Added resolution step to case
        ✓ Action: Isolated systems 192.168.50.10 and 192.168.50.15
        ✓ Result: Systems removed from network, investigation continuing

You: "Update case status to 'investigating' and assign to me"

Claude: ✓ Updated case-20260121-def456
        ✓ Status: investigating
        ✓ Assigned to: [your username]
```

## Technical Details

### How It Works

1. **Natural Language Processing**: Claude interprets your intent from natural language
2. **Context Awareness**: Claude tracks which finding/case you're examining
3. **Automatic Tool Selection**: Claude picks the right MCP tool for your request
4. **Parallel Execution**: Multiple operations happen simultaneously when possible
5. **Confirmation**: Claude confirms what was done and shows the results

### MCP Tool Architecture

```
User Request
    ↓
Claude (Natural Language Understanding)
    ↓
MCP Tool Selection
    ↓
deeptempo-findings MCP Server
    ↓
Database (PostgreSQL)
    ↓
Result Confirmation to User
```

### Extending the System

You can add more case management tools by:

1. Adding new functions to `tools/deeptempo_findings.py`
2. Using the `@mcp.tool()` decorator
3. Following the existing patterns for error handling and JSON responses
4. Restarting the MCP server to load new tools

## Troubleshooting

### "Case not found"
- Verify the case ID format: `case-YYYYMMDD-xxxxxxxx`
- Use `list_cases()` to see available cases

### "Finding not found"  
- Verify the finding ID format: `f-YYYYMMDD-xxxxxxxx`
- Use `list_findings()` to see available findings

### Changes not appearing
- Refresh the UI
- Check the case via `get_case(case_id)` to verify changes

### Tools not available
- Ensure MCP server is running: check `mcp-config.json`
- Verify `tools/deeptempo_findings.py` is accessible
- Restart backend services if needed

## Security & Audit Trail

All case modifications are:
- Timestamped with ISO 8601 format
- Logged in the database
- Trackable through case activities
- Reversible (findings can be removed, etc.)

Every action Claude takes on cases is recorded, providing a full audit trail of AI-assisted investigation.

## Related Documentation

- [API Documentation](./API.md) - REST API for programmatic access
- [Agents Guide](./AGENTS.md) - Using specialized SOC agents
- [MITRE ATT&CK Integration](./FEATURES.md#mitre-attck-integration) - Kill chain analysis
- [Case Management UI](./FEATURES.md#case-management) - Web interface for cases

## Summary

The chat-driven case management feature transforms how you investigate security incidents. Instead of manually creating cases, adding findings, and documenting your work, you can have a natural conversation with Claude and it will:

✓ Automatically add findings to cases
✓ Build kill chain timelines
✓ Tag MITRE techniques  
✓ Log all your investigation activities
✓ Document resolution steps
✓ Track the full investigation narrative

**Just investigate naturally and tell Claude what you're finding. It handles the rest.**

