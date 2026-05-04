# Architecture

## Overview

Vigil uses **backend tool integration via Claude Agent SDK**. No desktop application or separate MCP servers required for core functionality.

## Architecture Diagram

```
┌─────────────┐
│   Browser   │
│   (Web UI)  │
└──────┬──────┘
       │ HTTPS
       ↓
┌─────────────────────┐
│  FastAPI Backend    │
│  (Python)           │
│  ├── API Endpoints  │
│  ├── Tool Routing   │
│  └── Claude Service │
└──────┬──────────────┘
       │ Agent SDK
       ↓
┌─────────────────────┐
│  Anthropic Servers  │
│  (Claude 4.5)       │
│  Agent SDK          │
└──────┬──────────────┘
       │ Autonomous tool execution
       ↓
┌─────────────────────┐
│   Backend Tools     │
│  ├── Security       │
│  │   Detections    │
│  ├── Case Mgmt      │
│  ├── Approvals      │
│  └── ATT&CK         │
└──────┬──────────────┘
       │
       ↓
┌─────────────────────┐
│   Data Layer        │
│  ├── PostgreSQL     │
│  ├── Detection      │
│  │   Rules (6.7K)  │
│  └── Services       │
└─────────────────────┘
```

## Stack Components

### Frontend
- React/TypeScript
- Vite dev server
- Port: 6988

### Backend
- FastAPI (Python 3.10+)
- Claude API integration
- Backend tools (23 tools)
- Port: 6987

### Database
- PostgreSQL 16
- Findings, cases, embeddings
- Port: 5432

### Detection Rules
- 7,200+ rules (Sigma, Splunk, Elastic, KQL)
- Loaded from: `~/security-detections/`
- Indexed in memory on first use

## Tool Categories

### 1. Security Detection Tools (5)
Access to detection rule database:
- `analyze_coverage` - Coverage by MITRE technique
- `search_detections` - Keyword search across rules
- `identify_gaps` - Gap analysis
- `get_coverage_stats` - Statistics
- `get_detection_count` - Counts by source

### 2. Finding & Case Tools (10)
SOC investigation workflow:
- `list_findings` - Query findings
- `search_findings` - Keyword search across findings
- `get_findings_stats` - Finding statistics
- `get_finding` - Get specific finding
- `nearest_neighbors` - Similar findings
- `list_cases` - Query cases
- `get_case` - Get specific case
- `create_case` - Create new case
- `add_finding_to_case` - Associate findings
- `update_case` - Update case status and metadata
- `add_resolution_step` - Document resolution steps

### 3. MITRE ATT&CK Tools (2)
Technique mapping and visualization:
- `get_attack_layer` - Generate Navigator layer
- `get_technique_rollup` - Technique statistics

### 4. Approval Tools (5)
Autonomous response workflow:
- `list_pending_approvals` - Pending actions
- `get_approval_action` - Action details
- `approve_action` - Approve action
- `reject_action` - Reject action
- `get_approval_stats` - Statistics

**Total: 23 backend tools**

## Request Flow

### Example: Detection Coverage Query

```
1. User: "What's our coverage for T1059.001?"
   ↓
2. Web UI → POST /api/claude/chat
   ↓
3. Backend → Claude API (with tool definitions)
   ↓
4. Claude decides to use: analyze_coverage
   ↓
5. Backend executes: security_tools.analyze_coverage(["T1059.001"])
   ↓
6. Loads detection rules (if not cached)
   ↓
7. Searches Sigma, Splunk, Elastic, KQL rules
   ↓
8. Returns: { "T1059.001": { count: 234, by_source: {...} } }
   ↓
9. Claude synthesizes response
   ↓
10. Web UI displays result
```

## Configuration

### Environment Variables (.env)

```bash
# Claude API
ANTHROPIC_API_KEY=sk-ant-...

# Detection Rules
SIGMA_PATHS="${HOME}/security-detections/sigma/rules"
SPLUNK_PATHS="${HOME}/security-detections/security_content/detections"
ELASTIC_PATHS="${HOME}/security-detections/detection-rules/rules"
KQL_PATHS="${HOME}/security-detections/Hunting-Queries-Detection-Rules"

# Database
DATABASE_URL="postgresql://deeptempo:deeptempo_secure_password_change_me@localhost:5432/deeptempo_soc"
```

### Backend Tool Initialization

All backend API endpoints use:

```python
from services.claude_service import ClaudeService

claude_service = ClaudeService(
    use_backend_tools=True,   # ✅ Backend tools
    use_mcp_tools=False       # ❌ No MCP
)
```

## Deployment

### Development

```bash
# Terminal 1: Database
cd docker && docker-compose up -d postgres

# Terminal 2: Backend
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 6987 --reload

# Terminal 3: Frontend
cd frontend && npm run dev
```

### Production (Docker)

```bash
cd docker
docker-compose up -d
```

Includes:
- PostgreSQL
- Backend API
- Nginx (optional)

## Performance

### Tool Execution Latency

| Tool | Avg Latency |
|------|------------|
| Detection search | 100-500ms |
| Coverage analysis | 200-1000ms |
| Database queries | 10-50ms |
| ATT&CK layer | 200-500ms |

### Resource Usage

| Component | Memory | CPU |
|-----------|--------|-----|
| Backend | 500MB | 10-20% |
| Detection rules (loaded) | 200MB | 0% |
| PostgreSQL | 100MB | 5% |

### Scalability

- **Concurrent users**: 50-100 (single instance)
- **Detection rule index**: Shared across all users
- **Claude API**: Rate limited by Anthropic
- **Database**: PostgreSQL with connection pooling enabled

## Monitoring

### Health Endpoints

```bash
# Backend health
curl http://localhost:6987/api/health

# Claude API status
curl http://localhost:6987/api/claude/status

# Database connectivity
curl http://localhost:6987/api/storage/status
```

### Logs

```bash
# Backend logs (stdout when running uvicorn directly, or Docker logs)
docker logs deeptempo-postgres  # PostgreSQL logs
docker logs -f <backend-container>  # Backend API logs

# Tool execution (debug) — enable via LOG_LEVEL=DEBUG in .env
```

## Security

### API Key Management
- Stored in secure keychain (macOS/Linux)
- Fallback to environment variables
- Never logged or exposed

### Tool Permissions
- No dangerous operations exposed
- Approval required for response actions
- Rate limiting on API endpoints
- Authentication required (configurable)

## Comparison: Previous vs Current Architecture

### Before (MCP-Based)
```
Web UI → Backend → MCP Client → MCP Servers (stdio)
                                     ↓
                              Tool Implementations
```
- Required desktop application for local use
- Complex MCP server configuration
- Not web UI compatible
- Higher latency (protocol overhead)

### Now (Agent SDK)
```
Web UI → Backend → Claude Agent SDK → Backend Tools → Services
```
- Autonomous tool execution via Agent SDK
- Simple configuration
- Production-ready
- Lower latency

## Troubleshooting

### "No tools loaded"

```python
# Check tool initialization
from services.claude_service import ClaudeService
claude = ClaudeService(use_backend_tools=True)
print(f"Loaded: {len(claude.backend_tools)} tools")
# Should show: 19
```

### "Detection rules not found"

```bash
# Check repositories
ls -la ~/security-detections/
# Should show: sigma/, security_content/, detection-rules/, Hunting-Queries-Detection-Rules/

# Clone if missing
./scripts/setup_detection_repos.sh
```

### "Database connection failed"

```bash
# Check PostgreSQL
docker ps | grep postgres

# Check connection string
echo $POSTGRESQL_CONNECTION_STRING
```

## Development

### Adding New Tools

1. **Define tool schema** in `backend/schemas/tool_schemas.py`:

```python
{
    "name": "my_new_tool",
    "description": "What the tool does",
    "input_schema": {
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "..."}
        },
        "required": ["param1"]
    }
}
```

2. **Implement tool** in appropriate service

3. **Route tool** in `services/claude_service.py` → `_process_backend_tool_use()`:

```python
elif tool_name == 'my_new_tool':
    from services.my_service import MyService
    service = MyService()
    result = service.my_method(**arguments)
```

4. **Test**:

```bash
python tests/test_backend_tools.py
```

### Running Tests

```bash
# Backend tool tests
python tests/test_backend_tools.py

# Integration tests
python tests/test_integration_backend_tools.py

# Unit tests
pytest tests/unit/
```

## Documentation

- [Backend Tools Guide](BACKEND_TOOLS.md) - Detailed tool documentation
- [Detection Engineering](DETECTION_ENGINEERING.md) - Detection rule usage
- [Integrations](INTEGRATIONS.md) - Backend tool integration overview
- [API Reference](../backend/main.py) - FastAPI documentation

## Support

For issues or questions:
- GitHub Issues: https://github.com/Vigil-SOC/vigil/issues
- Label: `backend-tools`
- Include: Backend logs, tool name, error message

