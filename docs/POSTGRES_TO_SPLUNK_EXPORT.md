# PostgreSQL to Splunk Export Guide

This guide explains how to export data from your PostgreSQL database to Splunk using the HTTP Event Collector (HEC).

## Overview

The `export_postgres_to_splunk.py` script allows you to:
- Export **half** of your findings and cases from PostgreSQL to Splunk
- Send data directly to Splunk HEC or save to a JSON file
- Choose to export findings only, cases only, or both

## Prerequisites

### 1. Enable Splunk HTTP Event Collector (HEC)

First, you need to enable HEC in Splunk:

1. **Log into Splunk Web**
   - Navigate to your Splunk instance (e.g., `https://your-splunk:8000`)

2. **Enable HEC Globally**
   - Go to **Settings** → **Data Inputs** → **HTTP Event Collector**
   - Click **Global Settings**
   - Check "All Tokens" → **Enabled**
   - Set the HTTP Port (default: 8088)
   - Check "Enable SSL" (recommended)
   - Click **Save**

3. **Create a New HEC Token**
   - Click **New Token**
   - Give it a name (e.g., "DeepTempo Export")
   - Select source type: `json` or create custom source types like `deeptempo:finding` and `deeptempo:case`
   - Select the target index (e.g., `main` or create a dedicated index like `deeptempo`)
   - Click **Review** → **Submit**
   - **Copy the token value** - you'll need this!

### 2. Configure Environment (Optional)

You can add these to your `.env` file for convenience:

```bash
# Splunk HEC Configuration
SPLUNK_HEC_URL=https://your-splunk:8088/services/collector
SPLUNK_HEC_TOKEN=your-hec-token-here
SPLUNK_HEC_INDEX=deeptempo
```

### 3. Ensure Database is Running

Make sure your PostgreSQL database is running:

```bash
./scripts/start_database.sh
```

## Usage

### Basic Usage - Export Everything

Export half of all findings and cases to Splunk:

```bash
python scripts/export_postgres_to_splunk.py \
    --hec-url https://your-splunk:8088/services/collector \
    --hec-token your-hec-token-here \
    --index deeptempo \
    --no-verify-ssl
```

### Export Findings Only

```bash
python scripts/export_postgres_to_splunk.py \
    --hec-url https://your-splunk:8088/services/collector \
    --hec-token your-hec-token-here \
    --index deeptempo \
    --findings-only \
    --no-verify-ssl
```

### Export Cases Only

```bash
python scripts/export_postgres_to_splunk.py \
    --hec-url https://your-splunk:8088/services/collector \
    --hec-token your-hec-token-here \
    --index deeptempo \
    --cases-only \
    --no-verify-ssl
```

### Save to File Instead

If you want to review the data before sending or manually import it:

```bash
python scripts/export_postgres_to_splunk.py \
    --save-to-file postgres_export.json
```

Then manually upload to Splunk:
1. Go to **Settings** → **Add Data** → **Upload**
2. Select your JSON file
3. Choose sourcetype `json`
4. Select your index
5. Review and submit

## Command-Line Arguments

| Argument | Description | Default | Required |
|----------|-------------|---------|----------|
| `--hec-url` | Splunk HEC URL | None | Yes* |
| `--hec-token` | HEC authentication token | None | Yes* |
| `--index` | Target Splunk index | `main` | No |
| `--no-verify-ssl` | Disable SSL verification | False | No |
| `--findings-only` | Export only findings | False | No |
| `--cases-only` | Export only cases | False | No |
| `--batch-size` | Events per batch | 100 | No |
| `--save-to-file` | Save to file instead of sending | None | No |

\* Not required if using `--save-to-file`

## Data Format

### Findings

Each finding is exported with:
- `finding_id`: Unique identifier
- `data_source`: Origin of the finding
- `severity`: Severity level (critical, high, medium, low)
- `anomaly_score`: AI-detected anomaly score
- `mitre_predictions`: MITRE ATT&CK technique predictions
- `entity_context`: Related entities (IPs, users, hosts, etc.)
- `ai_enrichment`: AI-generated analysis
- `event_type`: "finding"
- `source_system`: "deeptempo_postgres"

### Cases

Each case is exported with:
- `case_id`: Unique identifier
- `title`: Case title
- `description`: Case description
- `status`: Current status (open, in_progress, closed, etc.)
- `priority`: Priority level (critical, high, medium, low)
- `assignee`: Assigned analyst
- `tags`: Case tags
- `mitre_techniques`: Associated MITRE techniques
- `timeline`: Case timeline events
- `activities`: Case activities
- `finding_ids`: Associated finding IDs
- `event_type`: "case"
- `source_system`: "deeptempo_postgres"

## Splunk Queries

Once data is exported, you can search it in Splunk:

### View All Exported Data

```spl
index=deeptempo source="postgresql_export"
```

### View Only Findings

```spl
index=deeptempo sourcetype="deeptempo:finding"
```

### View Only Cases

```spl
index=deeptempo sourcetype="deeptempo:case"
```

### High Severity Findings

```spl
index=deeptempo sourcetype="deeptempo:finding" severity="high" OR severity="critical"
| table _time, finding_id, severity, data_source, anomaly_score
```

### Cases by Status

```spl
index=deeptempo sourcetype="deeptempo:case"
| stats count by status
| sort -count
```

### Findings with High Anomaly Score

```spl
index=deeptempo sourcetype="deeptempo:finding" anomaly_score>0.8
| table _time, finding_id, severity, anomaly_score, mitre_predictions
```

### Timeline of Case Activity

```spl
index=deeptempo sourcetype="deeptempo:case"
| timechart count by priority
```

### Join Cases with Findings

```spl
index=deeptempo sourcetype="deeptempo:case"
| eval finding_id=mvindex(finding_ids, 0)
| join finding_id [
    search index=deeptempo sourcetype="deeptempo:finding"
    | fields finding_id, severity, anomaly_score
]
| table case_id, title, priority, finding_id, severity, anomaly_score
```

## Create Splunk Dashboards

### 1. Create a Dashboard

1. Go to **Search & Reporting**
2. Run one of the queries above
3. Click **Save As** → **Dashboard Panel**
4. Create a new dashboard or add to existing

### 2. Recommended Panels

- **Findings by Severity** (Pie Chart)
  ```spl
  index=deeptempo sourcetype="deeptempo:finding"
  | stats count by severity
  ```

- **Cases by Status** (Bar Chart)
  ```spl
  index=deeptempo sourcetype="deeptempo:case"
  | stats count by status
  ```

- **Anomaly Score Distribution** (Histogram)
  ```spl
  index=deeptempo sourcetype="deeptempo:finding"
  | bin anomaly_score span=0.1
  | stats count by anomaly_score
  ```

- **Top MITRE Techniques** (Table)
  ```spl
  index=deeptempo sourcetype="deeptempo:finding"
  | mvexpand mitre_predictions
  | stats count by mitre_predictions
  | sort -count
  | head 10
  ```

## Troubleshooting

### Error: "Authentication failed"

- Check that your HEC token is correct
- Ensure HEC is enabled in Splunk (Settings → Data Inputs → HTTP Event Collector → Global Settings)
- Verify the HEC URL is correct (should end with `/services/collector`)

### Error: "Connection refused"

- Check that Splunk is running
- Verify the HEC port (default: 8088)
- If using SSL, ensure the port is correct (usually 8088 for HEC)
- Try with `--no-verify-ssl` if you have a self-signed certificate

### Error: "Invalid data format"

- The script automatically formats data for HEC
- Check Splunk logs: `index=_internal sourcetype=splunkd HEC`

### Error: "No findings to export"

- Ensure you have data in PostgreSQL
- Run: `psql -d deeptempo_soc -c "SELECT COUNT(*) FROM findings;"`

### Large Export Taking Too Long

- Use smaller batch sizes: `--batch-size 50`
- Export to file first and review: `--save-to-file`
- Consider exporting findings and cases separately

## Performance Tips

1. **Batch Size**: Adjust `--batch-size` based on your network and Splunk capacity
   - Smaller batches (50-100): More reliable, slower
   - Larger batches (500-1000): Faster, may timeout

2. **Network**: Ensure good network connectivity between your system and Splunk

3. **Index**: Create a dedicated index for DeepTempo data to improve search performance

4. **Retention**: Configure index retention policies based on your needs

## Next Steps

After exporting data to Splunk:

1. **Create Alerts**: Set up Splunk alerts for high-severity findings
2. **Build Dashboards**: Visualize your security data
3. **Correlation**: Correlate PostgreSQL data with other Splunk data sources
4. **Reports**: Schedule regular reports for management
5. **Machine Learning**: Use Splunk ML toolkit for additional insights

## Support

For issues or questions:
- Check the logs: Look at the script output for detailed error messages
- Review Splunk HEC logs: `index=_internal sourcetype=splunkd HEC`
- Verify database connectivity: `./scripts/start_database.sh`

## Example Workflow

Here's a complete example workflow:

```bash
# 1. Ensure database is running
./scripts/start_database.sh

# 2. Export data to file for review (optional)
python scripts/export_postgres_to_splunk.py --save-to-file review_export.json

# 3. Export to Splunk
python scripts/export_postgres_to_splunk.py \
    --hec-url https://splunk.example.com:8088/services/collector \
    --hec-token 12345678-1234-1234-1234-123456789012 \
    --index deeptempo \
    --no-verify-ssl

# 4. Verify in Splunk
# Open Splunk Web and run:
# index=deeptempo | stats count by sourcetype

# 5. Create visualizations
# Use the SPL queries provided above
```

