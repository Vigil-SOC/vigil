# Backend Tool Integration via Agent SDK

## Overview

The Vigil platform supports **backend tool integration** via Claude Agent SDK. This enables autonomous tool execution through the Claude API with no desktop dependency.

## Architecture

```
User Browser
    ↓
Web UI (FastAPI)
    ↓
Claude Agent SDK (Anthropic Servers)
    ↓ (autonomous tool execution)
Backend Tools (Python)
    ↓
Database / Detection Rules / Services
```

### Key Benefits

- ✅ **Agent SDK Integration** - Autonomous tool execution via Claude
- ✅ **Web UI compatible** - All tools accessible via browser
- ✅ **Production ready** - Suitable for multi-user deployments
- ✅ **Lower latency** - Direct function calls through Agent SDK
- ✅ **Easier deployment** - No separate server configuration

## Available Tools

### Security Detection Tools (5)

Access to 7,200+ detection rules across Sigma, Splunk, Elastic, and KQL formats:

- **analyze_coverage** - Analyze detection coverage for MITRE ATT&CK techniques
- **search_detections** - Search detection rules by keywords
- **identify_gaps** - Identify detection gaps for threat contexts
- **get_coverage_stats** - Get overall coverage statistics
- **get_detection_count** - Get detection counts by source format

### Finding & Case Tools (11)

Interact with security findings and cases:

- **list_findings** - List findings with filters (severity, data source)
- **search_findings** - Keyword search across findings
- **get_findings_stats** - Finding statistics and aggregations
- **get_finding** - Get detailed finding information
- **nearest_neighbors** - Find similar findings via embedding search
- **list_cases** - List investigation cases
- **get_case** - Get detailed case information
- **create_case** - Create new investigation case
- **add_finding_to_case** - Add finding to case
- **update_case** - Update case status, priority, assignee, and metadata
- **add_resolution_step** - Document a resolution step with description, action, and result

### MITRE ATT&CK Tools (2)

Generate and analyze ATT&CK Navigator layers:

- **get_attack_layer** - Generate ATT&CK Navigator layer JSON
- **get_technique_rollup** - Get technique statistics from findings

### Approval Workflow Tools (5)

Manage autonomous response actions:

- **list_pending_approvals** - List actions awaiting approval
- **get_approval_action** - Get specific action details
- **approve_action** - Approve pending action
- **reject_action** - Reject pending action
- **get_approval_stats** - Get approval statistics

## Usage

### Enable Backend Tools with Agent SDK

When initializing the Claude service, enable Agent SDK and backend tools:

```python
from services.claude_service import ClaudeService

# Enable Agent SDK with backend tools (recommended for web UI)
claude = ClaudeService(
    use_backend_tools=True,
    use_mcp_tools=False,
    use_agent_sdk=True,
    enable_thinking=False
)
```

### Example Queries

#### Security Detection Analysis

```python
response = claude.chat(
    message="What's our detection coverage for PowerShell techniques T1059.001 and T1059.003?",
    max_tokens=2048
)
```

#### Finding Investigation

```python
response = claude.chat(
    message="Show me all high-severity findings from the last 24 hours",
    max_tokens=2048
)
```

#### Gap Analysis

```python
response = claude.chat(
    message="Analyze our detection gaps for ransomware attacks and recommend priorities",
    max_tokens=4096
)
```

#### Case Management

```python
response = claude.chat(
    message="Create a case for all findings related to IP 192.168.1.100",
    max_tokens=2048
)
```

## Implementation Details

### Tool Execution Flow

1. User sends message via web UI
2. FastAPI backend forwards to Claude API with tool definitions
3. Claude decides which tools to call based on user intent
4. Backend executes tool functions directly (no MCP)
5. Tool results returned to Claude for synthesis
6. Final response sent to user

### Tool Definitions

Tool schemas are defined in `backend/schemas/tool_schemas.py`:

```python
from backend.schemas.tool_schemas import ALL_TOOLS

# Contains 23 tools total:
# - 5 security detection tools
# - 11 findings/case tools
# - 2 MITRE ATT&CK tools
# - 5 approval tools
```

### Tool Routing

Tool execution is handled in `services/claude_service.py`:

```python
async def _process_backend_tool_use(self, content: List) -> List[Dict]:
    """Process tool use requests and call backend tools directly."""
    # Routes tool calls to appropriate service methods
    # No MCP protocol overhead
```

## Configuration

### Detection Rule Paths

Set environment variables for detection rule locations:

```bash
# In .env file
SIGMA_PATHS="${HOME}/security-detections/sigma/rules"
SPLUNK_PATHS="${HOME}/security-detections/security_content/detections"
ELASTIC_PATHS="${HOME}/security-detections/detection-rules/rules"
KQL_PATHS="${HOME}/security-detections/Hunting-Queries-Detection-Rules"
```

### Database Configuration

Backend tools use the existing database configuration:

```bash
# In .env file
DATABASE_URL="postgresql://deeptempo:deeptempo_secure_password_change_me@localhost:5432/deeptempo_soc"
```

## Testing

### Run Tests

```bash
# Basic tool tests
python tests/test_backend_tools.py

# Comprehensive integration tests
python tests/test_integration_backend_tools.py
```

### Expected Results

- All 23 tools should load successfully
- Coverage analysis should work with 7,200+ rules
- Finding/case queries should work with database
- Approval workflow should work with pending actions

## Comparison: Agent SDK vs MCP

| Feature | Agent SDK (Backend Tools) | MCP Servers |
|---------|---------------------------|-------------|
| **Deployment** | Single Python process | Requires desktop app + servers |
| **Web UI** | ✅ Fully supported | ❌ Not accessible |
| **Latency** | Lower (Agent SDK) | Higher (protocol overhead) |
| **Configuration** | Simple (env vars) | Complex (MCP config) |
| **Multi-user** | ✅ Production ready | ❌ Desktop only |
| **Tool Count** | 23 core tools | 100+ (including external) |
| **Autonomy** | ✅ Autonomous execution | Manual tool use |

## Migration from MCP

### For Existing Users

If you're currently using MCP servers with a desktop application:

1. Agent SDK backend tools provide equivalent functionality
2. You can keep MCP for advanced local workflows
3. Web UI uses Agent SDK backend tools automatically
4. No changes needed to existing MCP config

### For New Deployments

1. Skip MCP setup entirely
2. Run setup_dev.sh to clone detection repos
3. Set environment variables
4. Agent SDK enabled by default with `use_agent_sdk=True`

## Troubleshooting

### Tools Not Loading

```python
# Check tool count
claude = ClaudeService(use_backend_tools=True)
print(f"Loaded: {len(claude.backend_tools)} tools")
# Should show 23
```

### Detection Rules Not Found

```bash
# Ensure detection repos are cloned
ls -la ~/security-detections/
# Should show: sigma/, splunk/, elastic/, kql/

# Re-run setup if missing
./scripts/setup_detection_repos.sh
```

### Database Errors

```bash
# Check PostgreSQL is running
docker ps | grep postgres

# Check connection string
echo $DATABASE_URL

# Test connectivity
python -c "from database.connection import get_db; import asyncio; asyncio.run(get_db())"
```

## Performance

### Detection Rule Loading

- **First Load**: ~5-10 seconds (7,200+ rules)
- **Subsequent**: Cached in memory
- **Memory**: ~200MB for rule index

### Tool Execution

| Tool Type | Avg Latency |
|-----------|------------|
| Detection search | 100-500ms |
| Coverage analysis | 200-1000ms |
| Database queries | 10-50ms |
| Attack layer gen | 200-500ms |

### Optimization

- Detection rules are lazy-loaded
- Database queries use indexes
- Results are streamed when possible

## Future Enhancements

- [ ] Add caching layer for detection queries
- [ ] Implement Redis for cross-request caching
- [ ] Add more detection rule sources (YARA, Snort)
- [ ] Expand approval workflow tools
- [ ] Add bulk operations for findings/cases
- [ ] Implement tool usage analytics

## See Also

- [Detection Engineering Guide](DETECTION_ENGINEERING.md)
- [API Reference](API.md)
- [Integration Guide](INTEGRATIONS.md)
- [Web UI Documentation](WEB_UI.md)

