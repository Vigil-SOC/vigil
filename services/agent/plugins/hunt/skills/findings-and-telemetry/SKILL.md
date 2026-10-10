---
name: findings-and-telemetry
description: Broad behavioural hunting across the findings detection already scored and the raw telemetry behind them.
---

# Findings and telemetry

You have two surfaces and they answer different questions.

Vigil's findings are what detection already scored: one record per alert or
anomaly, carrying a severity, an anomaly score, predicted ATT&CK techniques and
the entities involved. Search them to learn what has already been noticed.

The telemetry search, when this deployment has one, reaches the raw records
behind those findings, in the SIEM's own query language. Go there for anything a
detection did not already summarise -- volumes, intervals, who talked to whom.

Start from the findings and go to the telemetry to characterise what you found.
A hypothesis that spans domains needs both.
