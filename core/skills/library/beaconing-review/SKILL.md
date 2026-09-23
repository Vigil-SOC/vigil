---
name: beaconing-review
description: Grade a periodic outbound flow as c2, suspicious, or benign-periodic. Read this when raw_data shows a repeating connection and you must separate a beacon from NTP, an update check, a monitoring agent, or telemetry.
metadata:
  vigil-origin: "epic #882 slice 2, #1074"
---

# Beaconing review

Grade one periodic outbound flow. Extract the facts, apply the first matching
rule, and finish with the labelled block. When no tool is callable in this
turn, do not announce a lookup and do not stop: reputation is `not checked`,
and the block is still written from the finding alone.

## Facts

From `raw_data`, take `src_ip`, the destination, `dst_port`, `bytes_sent`,
`bytes_received`, and `frequency`. The destination is `dst_ip` when that
field is present; otherwise `dst_host` or `dst_domain`. A domain or IP in
`iocs` does not replace `dst_ip`. The title and description are where a
source labels the flow as C2.

Note the source host's role from `src_ip` (an RFC 1918 address is an internal
client). Role explains who is phoning out. It does not pick the verdict.

Jitter is absent when `frequency` states one period. "every 60 seconds" and
"every 300 seconds" are fixed intervals with no jitter, including when the
text itself says "fixed" or "no jitter". A span ("40 to 110 seconds"), or
"roughly", "about", or an unnegated "jitter", means jitter is present.

## Reputation

Name a tool only when you hold it:

- If you hold `recall_entity`, call it on the destination before any external lookup.
- If you hold `lookup_indicators`, query the local feed for that indicator.
- If you hold `cf_lookup_ip_threat`, use it for an IP. If you hold `cf_lookup_domain_threat`, use it for a hostname.

A check you could not run is written `not checked`. Never invent a lookup
result. When the destination needs a full enrichment, read `ioc-enrichment`
and follow it. Do not restate that procedure here.

## Look-alikes

A fixed interval is normal for every row below. It does not make the flow a
beacon. A bare IP is none of these rows. Port 443 or 80 to a bare IP is not
an update check. Port 123 to a bare IP is not NTP.

- **NTP.** Hostname is an NTP pool or vendor time host (`pool.ntp.org`,
  `time.google.com`, `time.windows.com`, `time.cloudflare.com`), port 123,
  payloads small and symmetric (a few dozen bytes each way, including 48 and
  48). A fixed 300 second poll with no jitter still matches.
- **Update check.** Hostname is the vendor's update host
  (`windowsupdate.microsoft.com`, `update.microsoft.com`,
  `download.windowsupdate.com`, `archive.ubuntu.com`), port 80 or 443, client
  payload small. A fixed interval, including 300 seconds, still matches.
  Symmetric small payloads match this row too.
- **Monitoring agent.** Hostname is the vendor's monitor (Datadog, New Relic,
  Elastic) and payloads are small check-ins on that vendor's usual port.
- **Telemetry.** Hostname is the vendor's own telemetry host and payloads are
  small regular posts.

## Rules

Apply the first rule whose conditions all hold. Stop. The verdict value is
one lowercase token: `c2`, `suspicious`, or `benign-periodic`. Do not
capitalise the token. Do not pick `suspicious` when an earlier rule matched.

1. **benign-periodic** when the destination is a hostname (not a bare IP) and
   one look-alike row matches on hostname, port, and payload size. Small means
   each direction is under 10 KB. Symmetric means neither direction is more
   than about four times the other. A fixed interval with no jitter still
   qualifies. This rule wins over a fixed-interval suspicion.

2. **c2** when all four of these hold and rule 1 did not. The interval is
   fixed with no jitter. The destination is a bare IP. The port is not one
   the destination justifies: 443 or 80 to a bare IP does not justify it.
   The title or description labels the flow as C2 or "known C2
   infrastructure" — those words, not a guess from the port. A fixed
   interval with no jitter, to a bare IP, on a port the destination does
   not justify, and already labelled as C2 infrastructure by the source, is
   `c2`, not `suspicious`. Reputation `not checked` does not downgrade it.
   Confidence is high. The next step is contain.

3. **suspicious** otherwise: a jittered interval, an unremarkable cloud IP,
   or payloads that match no look-alike. Confidence is low or medium. The
   next step is hunt or monitor.

## Output

Write this block first, then at most four sentences. Five lines, each
starting at column 0 with the lowercase label, an ASCII colon, and one
space. No markdown bold, bullets, quotes, or backticks on these lines. No
JSON and no table. Replace every placeholder: the character after the space
is the value itself, not `<`. Do not leave angle brackets or pipe-separated
choices in the answer. Every answer has all five lines, including when a
fact is unknown.

The destination value is the flow endpoint alone: `dst_ip` when that field
is present, copied unchanged, otherwise the hostname. Do not append a port,
a domain from `iocs`, or a word.

`periodicity` states the interval and whether jitter is present or absent,
or `unknown`. `confidence` is `high`, `medium`, or `low`. `next` is `hunt`,
`contain`, `monitor`, or `dismiss`.

```
verdict: <c2|suspicious|benign-periodic>
periodicity: <interval and jitter, or unknown>
destination: <value>
confidence: <high|medium|low>
next: <hunt|contain|monitor|dismiss>
```
