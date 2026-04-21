# Sandbox & Malware Detonation

Vigil integrates with four sandboxes:

| Sandbox | Hosting | MCP source |
|---|---|---|
| Hybrid Analysis | cloud (Falcon) | `tools/hybrid_analysis.py` |
| Any.Run | cloud | `tools/anyrun.py` |
| Joe Sandbox | cloud or on-prem | external MCP (`joesandboxMCP`) wired via `mcp-config.json` |
| CAPE Sandbox | self-hosted (bring your own) | `tools/cape_sandbox.py` (new in #86) |

The Malware Analyst agent loads all four. The daemon has an opt-in
auto-submission pipeline that ties sandbox output back to findings and
cases.

## Architecture

```
┌────────────────┐      ┌──────────────────────┐      ┌──────────────────┐
│  Poller/SIEM   │─────▶│ FindingProcessor     │─────▶│ Response queue   │
│  (finding in)  │      │ ._enrich_finding()   │      │ Investigation q. │
└────────────────┘      │   • VirusTotal       │      └──────────────────┘
                        │   • Shodan           │
                        │   • Sandbox submit ◀─┼──── daemon/sandbox_submitter.py
                        └──────────────────────┘            (safety gates +
                                                             hash-cache lookup)
                                 │
                                 ▼
                        enrichment.sandbox_submissions = {hash: {cape:{task_id}}}
                                 │
                                 ▼  (every SANDBOX_POLL_INTERVAL s)
                        daemon/sandbox_poller.py
                                 │
                                 ├─ fetches completed report
                                 ├─ writes back to finding.enrichment.sandbox_reports
                                 └─ services/sandbox_correlation_service.py
                                         │
                                         ▼
                                CaseEvidence (analysis_results = raw report)
                                CaseIOC      (ips, domains, hashes, mutexes)
```

## Configuration

All env vars live in `env.example`. Minimum for CAPE:

```bash
CAPE_SANDBOX_ENABLED=true
CAPE_SANDBOX_URL=http://cape.internal:8000
CAPE_SANDBOX_API_KEY=<token>
```

Enable the pipeline (off by default):

```bash
SANDBOX_AUTO_SUBMIT=true
```

Safety knobs (defaults shown):

```bash
SANDBOX_MAX_FILE_SIZE_MB=100
SANDBOX_ALLOWED_FILE_TYPES=exe,dll,doc,docx,xls,xlsx,pdf,js,vbs,ps1,bat,msi
SANDBOX_ANALYSIS_TIMEOUT=300   # seconds before a submission is marked expired
SANDBOX_POLL_INTERVAL=60       # seconds between poller passes
```

## Safety posture

- **Opt-in**: the master `SANDBOX_AUTO_SUBMIT` switch defaults to `false`.
- **Hash-only**: Vigil never uploads binary bytes. Submission is via hash
  search against the sandbox's own cache. A sandbox returning "unknown"
  means a human operator (or the agent, via the MCP tool) must upload the
  sample manually.
- **Allowlist + size cap**: safety gating lives in
  `daemon/sandbox_submitter.py::is_hash_safe_to_submit`.
- **API-only transport**: no shared filesystems between Vigil and the
  sandbox; all traffic is HTTPS REST.

## Result correlation

Completed reports are written into existing tables — no schema changes:

- `case_evidence` row per `(case_id, sandbox, task_id)` with
  `evidence_type="sandbox_report"` and the raw report in
  `analysis_results`.
- `case_iocs` rows for network and dropped-file indicators. Each IOC's
  `enrichment_data.sandbox_runs[]` records which sandbox/task observed it
  (dedup + merge on re-observation).
- Reputation/threat-level fields on `CaseIOC` are derived from the
  sandbox's malscore via `_score_to_threat_level` /
  `_score_to_confidence`.

## Future work

- **Self-hosted CAPE via docker-compose.** CAPE needs KVM and Windows
  guest VMs, which don't fit cleanly inside Docker Desktop. A production
  deployment typically runs CAPE on a bare-metal or dedicated Linux host
  with nested virtualisation. We document `CAPE_SANDBOX_URL` as external
  and leave the infra outside this repo.
- **Binary upload path.** When the daemon gains access to the original
  file bytes (e.g. via email attachment extraction or network capture
  ingestion), add a second branch to `SandboxSubmitter.submit_file` that
  uploads via `cape_submit_file` / `ha_submit_file`.
- **Signature merge with MITRE Analyst.** Sandbox reports surface
  `mitre_techniques`; the correlation service records them on evidence,
  but the MITRE Analyst agent does not yet read them back.
