---
name: investigate-case
description: Method for working one open case from its findings to a supported disposition. Load at the start of a case.
---

# Investigating a case

## 1. Read what the case holds

Start with the case's own records and the findings attached to it. Note every
entity they name: hosts, identities, IPs, domains, hashes, processes. Those are
your pivots. Note the time window the findings cover.

## 2. Say what could explain it

Write down two or three explanations before you query: at least one benign and
one malicious. Each should predict something you can check. A lockout storm,
for example, is a stale credential on a scheduled task (one source host, a
regular interval, one account) or password spraying (many accounts, many
sources, a short burst).

## 3. Check the predictions

Query for what would separate the explanations, not for more of what you have.

- Widen the window before you trust an empty search: the activity may sit just
  outside it.
- Ask the telemetry, not only the findings: detection only scored what it saw.
- Look an observable up in the indicator database before you call it clean.
- Search the findings for the same entity on other hosts or identities: one
  case is often part of a larger one.

## 4. Decide what you can support

- **malicious**: a cited result shows attacker activity.
- **suspicious**: results point toward it but do not show it.
- **benign**: a cited result positively shows a legitimate cause.
- **false_positive**: the detection fired on something that is not what it
  looks for, and a cited result shows what it actually was.
- **inconclusive**: the tools could not separate the explanations. Say which
  query would.

Benign and false_positive are reviewed before they stand. Absence of results
does not support either one.

## 5. Write it so a reader can act

Lead with what happened. Cite the refs you rest on. List every gap with the
reason it stayed open. Next steps go most important first, and each names who
or what it touches.
