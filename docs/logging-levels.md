# Logging levels

Which level a log line gets is decided by what an operator should do about it, not by how surprising the line is. Anyone filtering on "ERROR and above" (an operator, a log-shipping alert, a support bundle summary) should see every outage and nothing else.

| Level | Meaning |
|---|---|
| **ERROR** | An operator should act: a dependency is down, data was lost or dropped, work is silently not being done, or a control is failing open. |
| **WARNING** | Degraded, but recovering by itself (a retry still within budget, a fallback with no loss), or a setup gap an operator has yet to close. |
| **INFO** | State changes and lifecycle events, and expected outcomes such as "not found". |
| **DEBUG** | Diagnostics only. Never the *only* record of a failure. |

## Rules

- **Repeating failures.** A condition that recurs every tick or request is logged once, at its level, when it starts. Later occurrences are rate-limited with a count, and one line is logged when it recovers. `core/integrations/_base/config_gap.py` does this for unfinished integration config.
- **Expected client outcomes** are not ERROR. A 404 "not found" and a config the operator hasn't finished yet are the caller's or operator's input, not a fault in Vigil. If the outcome already reaches the caller as a return value or HTTP status, the log line is not the only record.

Log field names, never secret values.
