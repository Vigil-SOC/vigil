# Splunk Testing Guide

This guide explains how to generate comprehensive test data for Splunk and test the Claude AI integration.

## Overview

The testing framework includes:
- **Test Data Generator**: Creates realistic security events
- **Integration Tester**: Tests end-to-end Splunk + Claude workflow
- **280+ Security Events**: Covering 7 attack categories

## Test Data Categories

The generator creates the following types of security events:

| Category | Count | Description |
|----------|-------|-------------|
| **Brute Force** | 50 | Failed authentication attempts from external IPs |
| **Malware** | 30 | Antivirus detections with file hashes |
| **C2 Traffic** | 100 | Command & control beaconing to malicious domains |
| **Data Exfiltration** | 20 | Large data transfers to external domains |
| **Privilege Escalation** | 15 | User account manipulation and admin access |
| **Lateral Movement** | 25 | Network logons and remote access |
| **Reconnaissance** | 40 | Port scanning and service enumeration |
| **Total** | **280** | |

## Generated IOCs (Indicators of Compromise)

Each event includes realistic indicators:

- **8 External Attacker IPs** (non-RFC1918)
- **8 Internal Victim IPs** (RFC1918)
- **8 Malicious Domains** (C2 servers, phishing sites, etc.)
- **7 Compromised Usernames**
- **6 Affected Hostnames**
- **Multiple File Hashes** (MD5, SHA256)
- **MITRE ATT&CK Mappings** (tactics and techniques)

## Quick Start

### Method 1: Generate Data Only (JSON File)

Generate test data and save to a JSON file:

```bash
python scripts/generate_splunk_test_data.py
```

This creates `splunk_test_data.json` with 280 events.

**Import into Splunk manually:**
1. Go to Splunk Web → Settings → Add Data → Upload
2. Select `splunk_test_data.json`
3. Set sourcetype to `json`
4. Choose your index (e.g., `main`)
5. Review and submit

### Method 2: Send Directly to Splunk HEC

Send test data directly to Splunk via HTTP Event Collector:

```bash
python scripts/generate_splunk_test_data.py \
    --send-to-splunk \
    --hec-url https://your-splunk-server:8088/services/collector \
    --hec-token your-hec-token-here \
    --index main \
    --no-verify-ssl
```

**Prerequisites:**
1. Enable HTTP Event Collector in Splunk:
   - Settings → Data Inputs → HTTP Event Collector
   - Click "Global Settings" → Enable
   - Create a new token
2. Note the HEC URL and token

### Method 3: Full Integration Test

Test the complete workflow (generate data → create case → enrich with Splunk → analyze with Claude):

```bash
# 1. Generate test data and create case
python scripts/test_splunk_claude_integration.py \
    --generate-data \
    --create-case

# 2. Enrich the case with Splunk data and Claude analysis
python scripts/test_splunk_claude_integration.py \
    --enrich-case <case_id_from_step_1> \
    --lookback-hours 24
```

**Prerequisites:**
- Splunk configured in `.env`:
  ```
  SPLUNK_URL=https://your-splunk:8089
  SPLUNK_USERNAME=admin
  SPLUNK_PASSWORD=your_password
  ```
- Claude API key in `.env`:
  ```
  ANTHROPIC_API_KEY=sk-ant-api03-...
  ```
- Database running (`./scripts/start_database.sh`)

## Script Reference

### generate_splunk_test_data.py

Generates realistic security event data for Splunk.

**Usage:**
```bash
python scripts/generate_splunk_test_data.py [options]
```

**Options:**
- `-o, --output FILE`: Output JSON file (default: `splunk_test_data.json`)
- `--send-to-splunk`: Send data directly to Splunk HEC
- `--hec-url URL`: Splunk HEC URL
- `--hec-token TOKEN`: Splunk HEC authentication token
- `--index INDEX`: Target Splunk index (default: `main`)
- `--no-verify-ssl`: Disable SSL verification

**Examples:**

```bash
# Generate data to file
python scripts/generate_splunk_test_data.py -o test_events.json

# Send to Splunk
python scripts/generate_splunk_test_data.py \
    --send-to-splunk \
    --hec-url https://splunk.example.com:8088/services/collector \
    --hec-token abcd1234-5678-90ef-ghij-klmnopqrstuv \
    --index security_test \
    --no-verify-ssl
```

### test_splunk_claude_integration.py

Tests the complete Splunk + Claude integration workflow.

**Usage:**
```bash
python scripts/test_splunk_claude_integration.py [options]
```

**Options:**
- `--generate-data`: Generate new test data
- `--send-to-splunk`: Send generated data to Splunk HEC
- `--hec-url URL`: Splunk HEC URL
- `--hec-token TOKEN`: Splunk HEC token
- `--create-case`: Create test case and findings from data
- `--enrich-case CASE_ID`: Enrich existing case with Splunk data
- `--lookback-hours HOURS`: Hours to look back in Splunk (default: 168)
- `--use-existing-data FILE`: Use existing test data JSON file

**Examples:**

```bash
# Generate data and create case (offline test)
python scripts/test_splunk_claude_integration.py \
    --generate-data \
    --create-case

# Use existing data file
python scripts/test_splunk_claude_integration.py \
    --use-existing-data splunk_test_data.json \
    --create-case

# Enrich specific case
python scripts/test_splunk_claude_integration.py \
    --enrich-case CASE-2026-001 \
    --lookback-hours 48

# Full workflow with Splunk upload
python scripts/test_splunk_claude_integration.py \
    --generate-data \
    --send-to-splunk \
    --hec-url https://splunk:8088/services/collector \
    --hec-token your-token \
    --create-case \
    --lookback-hours 24
```

## Complete Workflow Example

Here's a complete end-to-end testing workflow:

### Step 1: Setup Environment

```bash
# Ensure database is running
./scripts/start_database.sh

# Configure Splunk credentials
cat >> .env << EOF
SPLUNK_URL=https://your-splunk-server:8089
SPLUNK_USERNAME=admin
SPLUNK_PASSWORD=your_password
ANTHROPIC_API_KEY=sk-ant-api03-your-key
EOF
```

### Step 2: Generate and Upload Test Data

```bash
# Generate 280 security events
python scripts/generate_splunk_test_data.py

# Upload to Splunk (manual or HEC)
# Option A: Manual upload via Splunk Web UI
# Option B: Send via HEC
python scripts/generate_splunk_test_data.py \
    --send-to-splunk \
    --hec-url https://splunk:8088/services/collector \
    --hec-token YOUR_TOKEN \
    --no-verify-ssl
```

### Step 3: Create Test Case

```bash
# Create case with findings from test data
python scripts/test_splunk_claude_integration.py \
    --use-existing-data splunk_test_data.json \
    --create-case

# Note the case ID from output (e.g., CASE-2026-001)
```

### Step 4: Enrich Case with Splunk + Claude

```bash
# Enrich the case
python scripts/test_splunk_claude_integration.py \
    --enrich-case CASE-2026-001 \
    --lookback-hours 24

# This will:
# 1. Extract IOCs from the case
# 2. Query Splunk for each IOC
# 3. Send data to Claude for AI analysis
# 4. Update case with enrichment notes
# 5. Save results to files
```

### Step 5: Review Results

The enrichment creates two files:

1. **`enrichment_result_<case_id>.json`**: Full enrichment data including:
   - Extracted indicators (IPs, domains, hashes, etc.)
   - Splunk query results
   - Event counts and statistics
   - Claude analysis

2. **`claude_analysis_<case_id>.txt`**: Full Claude AI analysis including:
   - Key findings from Splunk data
   - Correlations between events
   - Timeline of attack activities
   - Risk assessment
   - Recommended next steps

### Step 6: View in UI

```bash
# Start the web interface
./start_web.sh

# Navigate to:
# http://localhost:3000
# 
# View:
# - Cases → Select your test case
# - Investigation tab → View graph and timeline
# - AI Insights → See Claude analysis
```

## Testing Different Scenarios

### Scenario 1: Brute Force Attack

Generate only brute force events:

```python
from scripts.generate_splunk_test_data import SplunkTestDataGenerator

generator = SplunkTestDataGenerator()
events = generator.generate_brute_force_events(100)
generator.save_to_file(events, "brute_force_test.json")
```

### Scenario 2: APT (Advanced Persistent Threat)

Generate full attack chain:

```python
generator = SplunkTestDataGenerator()
events = []
events.extend(generator.generate_reconnaissance_events(50))
events.extend(generator.generate_brute_force_events(30))
events.extend(generator.generate_privilege_escalation_events(20))
events.extend(generator.generate_lateral_movement_events(40))
events.extend(generator.generate_c2_traffic_events(100))
events.extend(generator.generate_data_exfiltration_events(30))
generator.save_to_file(events, "apt_attack.json")
```

### Scenario 3: Ransomware Attack

Focus on malware and C2:

```python
generator = SplunkTestDataGenerator()
events = []
events.extend(generator.generate_malware_events(50))
events.extend(generator.generate_c2_traffic_events(200))
events.extend(generator.generate_lateral_movement_events(30))
generator.save_to_file(events, "ransomware.json")
```

## Troubleshooting

### Splunk Connection Issues

```bash
# Test Splunk connection
python -c "
from services.splunk_service import SplunkService
from core.config import get_integration_config

config = get_integration_config('splunk')
splunk = SplunkService(
    server_url=config['url'],
    username=config['username'],
    password=config['password']
)
success, msg = splunk.test_connection()
print(f'Connection: {msg}')
"
```

### No Events Returned from Splunk

Possible causes:
1. Data not yet indexed (wait 1-2 minutes)
2. Wrong index specified
3. Time range too narrow
4. Splunk search permissions

**Solution:**
```bash
# Check if data is in Splunk
# In Splunk Web, run:
index=main sourcetype=json | head 100

# Or increase lookback time:
python scripts/test_splunk_claude_integration.py \
    --enrich-case CASE-2026-001 \
    --lookback-hours 168  # 7 days
```

### Claude API Errors

```bash
# Check API key
python -c "
from services.claude_service import ClaudeService
claude = ClaudeService()
if claude.has_api_key():
    print('✓ API key configured')
else:
    print('✗ API key missing')
"
```

## Event Structure

Each generated event has a consistent structure:

```json
{
  "_time": "2026-01-21T10:30:45",
  "sourcetype": "WinEventLog:Security",
  "source": "XmlWinEventLog:Security",
  "host": "WORKSTATION-10",
  "signature": "Multiple Failed Logon Attempts",
  "signature_id": "AUTH-001",
  "severity": "high",
  "urgency": "high",
  "src": "185.220.101.45",
  "src_ip": "185.220.101.45",
  "dest": "10.0.1.15",
  "dest_ip": "10.0.1.15",
  "user": "jsmith",
  "action": "failure",
  "rule_name": "Brute Force Attack",
  "rule_title": "Brute Force Attack Detected",
  "rule_description": "Multiple failed logon attempts detected",
  "category": "Authentication",
  "subcategory": "Brute Force",
  "mitre_tactic": "Credential Access",
  "mitre_technique": "T1110.003",
  "priority": "high",
  "_raw": "..."
}
```

## MITRE ATT&CK Coverage

The test data covers these MITRE ATT&CK tactics:

| Tactic | Techniques | Event Count |
|--------|------------|-------------|
| Initial Access | T1566.001, T1190, T1133 | 40 |
| Execution | T1059.001, T1059.003 | 30 |
| Persistence | T1543.003, T1547.001 | 15 |
| Privilege Escalation | T1548.002, T1134 | 15 |
| Defense Evasion | T1562.001, T1070.004 | 30 |
| Credential Access | T1110.003, T1003.001 | 50 |
| Discovery | T1083, T1046, T1087.001 | 40 |
| Lateral Movement | T1021.001, T1021.002 | 25 |
| Collection | T1005, T1039 | 20 |
| Command and Control | T1071.001, T1573 | 100 |
| Exfiltration | T1041, T1048.003 | 20 |
| Impact | T1486, T1490 | 30 |

## Best Practices

1. **Use Dedicated Test Index**: Create a separate Splunk index for test data
   ```bash
   # In Splunk Web:
   Settings → Indexes → New Index → "test_security"
   ```

2. **Time-bound Tests**: Use `--lookback-hours` to limit search scope
   ```bash
   --lookback-hours 24  # Last 24 hours only
   ```

3. **Clean Up Test Data**: Remove test cases after testing
   ```bash
   # Delete test cases via API or UI
   curl -X DELETE http://localhost:8000/api/cases/CASE-2026-001
   ```

4. **Monitor Claude Usage**: Claude API has rate limits and costs
   - Use `--use-existing-data` to avoid regenerating
   - Test with smaller datasets first

5. **Verify Results**: Always check the generated files
   ```bash
   cat enrichment_result_CASE-2026-001.json | jq '.splunk_data.summary'
   cat claude_analysis_CASE-2026-001.txt | head -50
   ```

## Next Steps

After successful testing:

1. **Configure Real Data Sources**: Set up actual SIEM integrations
2. **Create Custom Alerts**: Use the test data patterns to create detection rules
3. **Train Your Team**: Use test cases for analyst training
4. **Benchmark Performance**: Measure enrichment and analysis times
5. **Iterate**: Adjust test data to match your environment

## Support

For issues or questions:
- Check logs: `logs/backend.log`, `logs/daemon.log`
- Review documentation: `docs/INTEGRATIONS.md`, `docs/API.md`
- Open an issue on GitHub

---

**Happy Testing! 🚀**

