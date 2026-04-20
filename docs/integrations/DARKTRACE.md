# Darktrace Integration

Vigil accepts **pushed alerts** from Darktrace via signed HTTP webhooks. Both
Darktrace SaaS tenants and on-prem master appliances are supported — they
emit the same JSON payload shapes.

There is currently no MCP (agent-pull) tool for Darktrace; this cut is
push-only. Investigations that want to pivot back into Darktrace should
follow the console link stored on each ingested finding.

## Supported alert streams

| Stream | Endpoint |
|---|---|
| Model Breach Alerts | `POST /api/webhooks/darktrace/model-breach` |
| AI Analyst Incidents | `POST /api/webhooks/darktrace/ai-analyst` |
| System Status Alerts | `POST /api/webhooks/darktrace/system-status` |
| Health probe (GET) | `GET  /api/webhooks/darktrace/health` |

Each alert is transformed into a Vigil `finding` (`data_source = "darktrace"`)
and passed to the standard ingestion pipeline, so triage, correlation, and
case creation work identically to Splunk/CrowdStrike sources.

## Vigil configuration

Set in `.env`:

```
DARKTRACE_ENABLED=true
DARKTRACE_URL=https://<tenant>.cloud.darktrace.com    # or https://<on-prem-master>
DARKTRACE_WEBHOOK_SECRET=<random-32+-char-string>
# optional — hard cap on webhook body size (default 1024 KB)
DARKTRACE_MAX_BODY_KB=1024
```

Generate a secret:

```
openssl rand -hex 32
```

`DARKTRACE_URL` is optional. When set, every ingested finding gets an
`evidence_links` entry pointing back to the originating console (model
breach or AI Analyst incident URL).

## Darktrace configuration

1. In the Darktrace Threat Visualizer: **System Config → Workflow Integrations → Add** → choose **Custom Webhook** (or the specific integration if available for AI Analyst).
2. **URL**: `https://<vigil-host>:6987/api/webhooks/darktrace/model-breach` (repeat for `ai-analyst` and `system-status` — each endpoint is separate so you can wire each stream independently).
3. **Authentication**: select **HMAC-SHA256**. Paste the same value you used for `DARKTRACE_WEBHOOK_SECRET`.
4. **Signature header**: `X-Darktrace-Signature` — hex HMAC-SHA256 over the raw request body. Vigil also accepts the `sha256=...` prefix style.
5. Click **Test** — Vigil returns `202 Accepted` on success, `401` if the signature doesn't match.

### SaaS vs on-prem notes

- **SaaS** tenants cannot emit syslog over the public internet — webhooks are the only option. Make sure Vigil is reachable at a TLS-terminated, publicly-routable URL.
- **On-prem** masters work the same way; if your Vigil instance is only reachable internally, point Darktrace at the internal hostname. Syslog remains possible for on-prem deployments but is not used by this integration.

## Verifying end-to-end

1. Run Vigil's backend (`./start_web.sh` or `uvicorn backend.main:app --reload`).
2. Confirm the receiver is live:
   ```bash
   curl https://<vigil-host>:6987/api/webhooks/darktrace/health
   ```
3. Smoke test with a signed payload:
   ```bash
   BODY='{"pbid":12345,"model":{"name":"Device / Anomalous Connection"},"score":0.92,"device":{"ip":"10.0.0.5","hostname":"laptop-01"},"time":1712995200000}'
   SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$DARKTRACE_WEBHOOK_SECRET" -hex | awk '{print $2}')
   curl -X POST https://<vigil-host>:6987/api/webhooks/darktrace/model-breach \
     -H "Content-Type: application/json" \
     -H "X-Darktrace-Signature: $SIG" \
     -d "$BODY"
   ```
   Expect `HTTP 202` with `{"accepted": true, "finding_id": "f-YYYYMMDD-xxxxxxxx"}`.
4. Confirm ingestion: `curl https://<vigil-host>:6987/api/findings?data_source=darktrace`.

## Idempotency

Vigil derives each `finding_id` from the alert's stable identifier
(`pbid` for Model Breach, `uuid` for AI Analyst, `id`/`name` fallback for
System Status). Replaying the same webhook produces the same finding ID,
which the ingestion layer skips as a duplicate — safe to re-deliver.

## Security

- Every request body is HMAC-verified before any parsing. Unsigned/invalid
  requests are rejected with `401` and no content is touched.
- If `DARKTRACE_WEBHOOK_SECRET` is unset the receiver fails closed with
  `503` — it never ingests unauthenticated traffic.
- Bodies above `DARKTRACE_MAX_BODY_KB` are rejected with `413`.
- Expose the endpoint over TLS only; the signature protects integrity but
  not confidentiality.
