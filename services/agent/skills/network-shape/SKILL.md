---
name: network-shape
description: Traffic shape -- beaconing intervals, jitter, volume asymmetry, DNS and HTTP. Load when a hypothesis is about how hosts talk.
---

# Network shape

Query the network telemetry -- flows, HTTP and DNS -- in the query language of
whichever system this deployment runs. Findings carry what detection already
scored; the shape of traffic is not in them, so the telemetry is where these
answers are.

If this deployment exposes no telemetry search, say so as a gap in what could be
seen. Do not infer traffic shape from alert records.

You are looking at the shape of traffic, not its content. Characterise
periodicity and its jitter, bytes out against bytes in, connection counts per
destination, and the lifetime of a pair. A regular interval with low variance
is the signal; a busy host is not.

Quantify. "Every 300s +/- 4s over 6 hours, 412 connections" is a finding;
"frequent connections" is not.
