# VStrike network context

Enable **CloudCurrent VStrike** in **Settings → Integrations** and configure its URL,
username, and password. The Dashboard's **VStrike** tab opens the external graph.
The tab, inline finding/detail actions, and events button are shown only after
integration settings have loaded and VStrike is enabled. Disabling it closes the
graph and events drawer, cancels provider retries/polling, and returns an active
VStrike view to the preserved findings queue.
When several networks are available, choose one explicitly. Network and storyline
choices come from the configured account; no deployment or scenario is hard-coded.

A finding's inline **VStrike** action carries its source and destination IPv4
addresses into that view. The same action is available in finding details.
Missing, ambiguous, non-IPv4, or invalid endpoint pairs disable the action.
The view retains the requested addresses and offers **Retry selection**,
**Back to finding**, and **Back to results**. Search, filters, sorting, pagination,
column preferences, and table scroll survive this round trip. Existing CSV export
still exports the filtered and sorted queue.

**VStrike events** opens an on-demand drawer of external danger events for the
selected storyline. It reads at most 100 source events per page (API maximum 250)
and refreshes five seconds after each completed read, only while the drawer is
open. A full page is labeled as potentially truncated. Failed refreshes preserve
the last successful page with a stale-data message. These events are never stored
as Vigil findings and are never assigned model scores or Vigil severity.
Source times display in UTC; absent timestamps remain unavailable.

## Embedding and configuration

The graph is initialized lazily and its iframe survives Dashboard tab changes.
It is disposed when the Dashboard unmounts. A minimum 1280 × 720 internal viewport
avoids provider initialization failures in narrow containers; the view scales
within the available space. **Expand graph** provides more room.

The default Vigil Content Security Policy admits the configured VStrike origin
only in `frame-src`, on HTML responses. It does not grant that origin script or
API access to Vigil. A custom `VIGIL_CSP_POLICY` remains authoritative and must
explicitly allow the provider origin in `frame-src`. Credentials and transient
iframe tokens stay out of findings, URL navigation state, and browser storage.

## Provider contract and limits

The workflow uses `network-list`, `ui-login-token`, `ui-network-load`,
`storyline-list`, `ui-storyline-apply`, `ui-storyline-forward`,
`ui-storyline-backward`, `storyline-events-get`, and `ui-find-by-ip-then-zoom`.
The event/apply calls identify collections as `storylineSetId`. VCR steps have no
arguments. Unsupported selection tools return HTTP 501; other provider failures
return an error without exposing raw authenticated URLs.

An accepted MCP command is **not proof of rendered highlighting or zoom**. The UI
explicitly says selection was requested and remains unconfirmed. Exact matching,
camera framing, and path highlighting remain provider responsibilities. This
change does not fix provider prefix matching or promise an exact path highlight.
Use **Reconnect** if the embedded provider shows a login screen.

The iframe load event can precede the provider's WebSocket registration. Network
loading has one delayed attempt and one bounded retry; a matching state message
from the exact iframe origin and window cancels the retry. Selection is reapplied
after a retry, while outdated responses cannot override newer UI context.
