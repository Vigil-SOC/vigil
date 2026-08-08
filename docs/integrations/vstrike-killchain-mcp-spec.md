# VStrike MCP — `ui-killchain-replay` tool spec

This is the request to the CloudCurrent / VStrike engineering team for a new
MCP tool that lets Vigil drive kill-chain replays through the VStrike UI.

The Vigil side of the wire is already shipped. Once VStrike's MCP server
exposes `ui-killchain-replay`, Vigil's "Play" button (visible in the iframe
toolbar inside every Vigil case dialog) starts working with no further code
changes on Vigil's side. Until then, the button surfaces a friendly
"VStrike server doesn't yet implement kill-chain replay" notice.

## Why we need this

When a Vigil analyst opens a case enriched by VStrike, every finding's
`entity_context.vstrike` carries an `attack_path` (an ordered list of
asset IDs from initial access to the target) plus `adjacent_assets` (the
MITRE technique on each edge). Today, the user can see the topology in
the embedded VStrike iframe, but they can't replay the kill-chain across
those nodes without manually clicking through.

Vigil walks the case's findings, dedupes by asset, and ships VStrike a
clean step list. VStrike then animates that sequence — highlighting each
node, drawing edges as transitions, surfacing the MITRE technique labels.
One MCP tool, one WebSocket push to the active session, no iframe reload.

This pattern is identical to the existing `ui-network-load` tool — same
auth (JWT), same JSON-RPC transport, same SSE response framing, same
session-state assumption (the iframe is already open and authenticated).

## Tool definition

```jsonc
{
  "name": "ui-killchain-replay",
  "description": "Animate a kill-chain through the active VStrike UI session. The iframe receives a WebSocket message and walks the supplied step sequence — highlighting each node, drawing edges as transitions, and surfacing MITRE technique labels.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "networkId": {
        "type": "string",
        "description": "The network identifier to load before stepping. The tool MUST internally do a `ui-network-load` if the active session is on a different network."
      },
      "steps": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "properties": {
            "node_id":   { "type": "string",  "description": "Asset ID to highlight at this step. Must exist in networkId." },
            "timestamp": { "type": "string",  "description": "ISO-8601 timestamp of the originating event. Surfaced as a label." },
            "technique": { "type": "string",  "description": "Optional MITRE ATT&CK ID for the EDGE leading into this node (e.g. T1021.002)." },
            "label":     { "type": "string",  "description": "Optional human-readable annotation to display alongside the node during dwell." },
            "dwell_ms":  { "type": "integer", "description": "Optional per-step dwell override; default 2000 ms." }
          },
          "required": ["node_id", "timestamp"]
        }
      },
      "loop":      { "type": "boolean", "description": "If true, restart from step 0 after the last step (default false)." },
      "auto_play": { "type": "boolean", "description": "If false, the iframe shows the steps loaded but waits for the user's Play button (default true)." }
    },
    "required": ["networkId", "steps"]
  },
  "execution": { "taskSupport": "forbidden" }
}
```

### Patterns this borrows from `ui-network-load`

- **WebSocket push to the active session.** No iframe reload. Single tool
  call per replay. The MCP tool itself is stateless — VStrike's UI side
  consumes the message and animates.
- **Stateless server-side.** No per-replay session record. The MCP tool
  just routes the steps to whichever VStrike UI session matches the
  caller's JWT.
- **Returns a `content` text confirmation.** Same shape as the existing
  `ui-network-load` response — JSON-RPC `result.content[].text`, with
  `isError` as the failure signal so Vigil's existing extractor works
  unchanged.

## Open questions

These don't block shipping the tool, but they affect what the user sees:

1. **Edge animations.** Does the VStrike UI already render edge animations
   between sequential nodes? If not, the kill-chain replay would step one
   node at a time without showing the link visualization the analyst
   wants. Vigil's UX assumption is that VStrike draws the edge into each
   highlighted node. If that doesn't exist today, can it ship alongside
   the MCP tool?

2. **Playback-finished event.** Can the iframe send a "playback finished"
   message back over its existing WebSocket so Vigil can sync its
   EventTimeline scrubber? Bidirectional channel — nice-to-have, not a
   blocker. If you can emit this, Vigil will hook it up; if not, we'll
   add a dwell-sum estimate on Vigil's side.

3. **`node_id` format.** Vigil's findings carry asset identifiers that
   we believe match VStrike's `node-list` output (the `id` / `network_id`
   / `uuid` field, in that priority order). Please confirm. If `node_id`
   needs to be the same shape `node-list` returns per network, Vigil
   already produces that shape.

## Curl example — what Vigil sends

This is what a real call looks like once the tool ships. Vigil already
emits exactly this payload via `POST /api/integrations/vstrike/ui/killchain-replay`,
which translates to a `tools/call` JSON-RPC request against VStrike's
`/mcp` endpoint:

```bash
curl -X POST 'https://vstrike.example/mcp' \
  -H "Authorization: Bearer ${VSTRIKE_JWT}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1730000000000,
    "method": "tools/call",
    "params": {
      "name": "ui-killchain-replay",
      "arguments": {
        "networkId": "net-123",
        "steps": [
          { "node_id": "asset-001", "timestamp": "2026-04-28T11:00:00Z", "label": "Initial Access" },
          { "node_id": "asset-042", "timestamp": "2026-04-28T11:05:00Z", "technique": "T1021.002", "label": "Lateral movement → file-server" },
          { "node_id": "asset-077", "timestamp": "2026-04-28T11:12:00Z", "technique": "T1003.001", "label": "Target: domain-controller" }
        ],
        "loop": false,
        "auto_play": true
      }
    }
  }'
```

Expected success response (SSE-framed JSON-RPC, matching `ui-network-load`):

```
event: message
data: {"jsonrpc":"2.0","id":1730000000000,"result":{"content":[{"type":"text","text":"Replay queued: 3 steps, network net-123"}],"isError":false}}
```

## Contact

Direct questions about the Vigil-side implementation to:
- Vigil repo: <https://github.com/Vigil-SOC/vigil>
- Tool client lives in [`core/integrations/vstrike/client.py`](../../core/integrations/vstrike/client.py) (see
  `killchain_replay_in_ui`)
- API surface: [`services/api/routers/vstrike.py`](../../services/api/routers/vstrike.py) (`POST /ui/killchain-replay`)
- Frontend Play button: [`clients/web/src/components/graph/VStrikeIframeHost.tsx`](../../clients/web/src/components/graph/VStrikeIframeHost.tsx)
