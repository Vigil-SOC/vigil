# Lab 06 (Stretch) — Connect a Real Integration

**Time:** 30 minutes
**Goal:** Move off demo mode, wire up one real OSINT integration (VirusTotal), and validate that an agent invokes it correctly against real data.

This lab is optional. Do it if:

- You have an extra 30 minutes after Lab 05.
- The "integration story" is a key concern for your team's decision.
- You want to confirm that Vigil's claim of "30+ integrations" survives contact with reality.

---

## Prerequisites

- Lab 00–05 complete.
- A **free VirusTotal API key**: https://www.virustotal.com/gui/my-apikey (sign up, takes 2 minutes; free tier allows 4 requests/minute, plenty for this lab).

---

## Why VirusTotal

It's the lowest-friction integration to validate:

- Free tier exists, no procurement.
- Read-only (no destructive actions, so no approval workflow complications).
- Outputs are recognizable to any security engineer — you can eyeball whether the result is real.
- The Threat Intel agent ([services/soc_agents.py](../../services/soc_agents.py) → `threat_intel` config) has VirusTotal in its tool list, so the wiring is already there.

If your team's actual deciding integration is Splunk, CrowdStrike, or Azure Sentinel, do this lab anyway as a wiring sanity check, then plan a follow-up session to test your real integration. See [docs/INTEGRATIONS.md](../INTEGRATIONS.md) for the full catalog.

---

## Steps

### 1. Disable demo mode (3 min)

Demo mode short-circuits real data flow. Turn it off:

```bash
# Stop the running stack (Ctrl-C in the start_web.sh terminal)

# Edit .env
# Change: DEMO_MODE=true  →  DEMO_MODE=false

# Restart
./start_web.sh
```

Confirm:

```bash
curl -s http://localhost:6987/api/config/demo-mode | jq
# expect: { "enabled": false, ... }
```

Your dashboard will now be mostly empty — that's expected. We're about to put real data through it.

### 2. Add VirusTotal credentials (3 min)

Add to `.env`:

```
VIRUSTOTAL_API_KEY=your-vt-key-here
```

Then restart the backend so the MCP server picks it up. VirusTotal is in [mcp-config.json](../../mcp-config.json) — confirm it's listed (it's a community MCP server, the entry name will be something like `virustotal` or `vt`).

### 3. Confirm MCP wiring (2 min)

After restart, check the MCP tools list:

```bash
curl -s http://localhost:6987/api/claude/sdk-status | jq
# expect agent_sdk_available: true, anthropic_available: true
```

If there's a dedicated MCP tools endpoint, hit it and grep for VirusTotal-related tool names. Otherwise just proceed — if the wiring is broken, the next step will tell you.

### 4. Run Threat Intel against a real IOC (10 min)

Pick a known-bad IOC to enrich. Safe choices:

- The EICAR test file hash: `275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f` (SHA-256).
- Google's public DNS IP: `8.8.8.8` (boring but real — and confirms the integration works for non-malicious entries too).
- Any IP you know to be in a public threat feed.

**Do not use IOCs from your production environment in a free-tier VirusTotal account** — VirusTotal public submissions are visible to other users.

Fire the Threat Intel agent:

```bash
curl -X POST http://localhost:6987/api/claude/agent/task \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "threat_intel",
    "task": "Enrich the SHA-256 hash 275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f using VirusTotal. Report detection ratio, first-seen date, and notable AV vendor names that flagged it.",
    "max_turns": 8
  }' | jq
```

Read the response. Specifically look at:

- `tool_calls` — did the agent actually call a VirusTotal MCP tool? If `tool_calls` is empty or only shows non-VT tools, the wiring is broken.
- `result` — does it cite concrete VT data (vendor names, detection counts, first-seen dates)? If it's vague ("this hash is widely flagged as malware"), the agent confabulated and didn't actually call the tool.
- Latency — VT API calls have real network latency. A genuine tool call should add a noticeable second or two.

### 5. The end-to-end test: enrich during an investigation (10 min)

Now create a real finding that mentions an IP, ingest it, and run the IR workflow. This is the "does the integration plug into actual workflows" test, not just "does the tool fire in isolation."

```bash
# Create a finding with a real IP
curl -X POST http://localhost:6987/api/findings/ \
  -H 'Content-Type: application/json' \
  -d '{
    "id": "lab06-vt-test",
    "title": "Outbound connection to suspicious IP",
    "description": "Endpoint communicated with external IP, requires enrichment",
    "severity": "high",
    "source": "manual",
    "raw_data": { "src_ip": "10.0.0.50", "dst_ip": "8.8.8.8", "dst_port": 443 },
    "iocs": { "ips": ["8.8.8.8"] }
  }'

# Run full-investigation workflow (this includes Threat Intel)
curl -X POST http://localhost:6987/api/workflows/full-investigation/execute \
  -H 'Content-Type: application/json' \
  -d '{ "finding_id": "lab06-vt-test" }' | jq
```

Watch the run. The Threat Intel phase should reach out to VT and return real enrichment, which then informs the rest of the workflow.

### 6. Watch network traffic (optional, 2 min)

If you want hard proof the call is real, in a third terminal:

```bash
# macOS / Linux
sudo tcpdump -i any -n host virustotal.com 2>&1 | head -20
```

You should see actual HTTPS traffic to `virustotal.com` while the agent runs.

---

## Success criteria

- [ ] Demo mode is off, backend restarted, `/api/config/demo-mode` returns `enabled: false`.
- [ ] VirusTotal API key is configured.
- [ ] You ran the Threat Intel agent against a real IOC and confirmed it called the VT MCP tool (verified via `tool_calls` in the response, latency, or tcpdump).
- [ ] The agent's output contained **concrete VirusTotal data** (specific vendor names, specific numbers), not vague generalities.
- [ ] You ran a full workflow against a real finding and saw the Threat Intel phase enrich it with VT data.

---

## Findings to capture

1. **Wiring effort.** How many minutes from "I have a VT key" to "an agent used it"? Acceptable for your team?
2. **Confabulation check.** Did the agent ever claim VT data without actually calling the tool? (This is the most dangerous failure mode — agents that fake integrations because they can't reach them.)
3. **The integration you actually need.** Which integration from [docs/INTEGRATIONS.md](../INTEGRATIONS.md) is your top priority? Is it on the supported list, the community list, or missing?
4. **Gaps.** Anything missing from the integration setup that you'd consider a hard requirement (auth pattern, rate limiting, retries, observability)?

---

## Where to look next

- The full integration catalog and setup guides: [docs/INTEGRATIONS.md](../INTEGRATIONS.md).
- MCP configuration: [mcp-config.json](../../mcp-config.json).
- Threat Intel agent prompt: [services/soc_agents.py](../../services/soc_agents.py) → `threat_intel`.
- For your *real* integration of choice (Splunk, CrowdStrike, Sentinel, etc.), find its section in [docs/INTEGRATIONS.md](../INTEGRATIONS.md) and plan a follow-up wiring session.

---

You are now done with the lab series. Consolidate your "Findings to capture" notes from all six labs into a single eval doc for your team. The most valuable sections of that doc are:

1. **What we'd trust autonomously, and what we wouldn't.**
2. **The integration we need next that Vigil does or does not have.**
3. **Projected cost at our volume.**
4. **The one thing that would block production deployment for us today.**
