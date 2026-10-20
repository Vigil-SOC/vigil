# RCA report: t

Run t · alert a at 2026-04-02T00:00:00Z · exit **null**

**Disposition:** True Positive (root cause not fully confirmed): 2 confirmed upstream event(s) explain the alert.

**Accounts**

- the attacker (an operator the logs do not name): attacker (operated the malicious actions)
- j.doe: victim account, used by the attacker (an operator the logs do not name); the person is not the attacker
- devuser: legitimate user; its activity here was routine work the attack passed through; not the attacker

## 1. Verdict

Root-cause chain, symptom first (path score 0.79):

1. **N1** symptom — confirmed 0.88 (edr)
2. **N3** calendar entry deleted on host-b7 using j.doe's stolen session — confirmed 0.98 @ 2026-04-01T11:00:00Z (edr) ← link E1 0.98
3. **N2** devuser applied a config from 192.0.2.7 — confirmed 0.95 @ 2026-04-01T10:00:00Z (edr) *(legitimate activity, not the attacker)* ← link E2 0.98

## 2. Attribution

- **N2** devuser applied a config from 192.0.2.7: ran as devuser (confirmed 0.88)
- **N3** calendar entry deleted on host-b7 using j.doe's stolen session: ran as j.doe (confirmed 0.88); operated by intruder via the stolen session (suspected 0.54)

**Impersonation indicators**

- none found

**Exonerated identities**

- none

## Indicators (from confirmed and suspected events)

- ip: 198.51.100.9
- host: host-b7

## Incident timeline

- time unknown · **N1** symptom — confirmed 0.88 (edr) · chain
- 2026-04-01T09:00:00Z · **N4** connection from 203.0.113.5 to host-c3 — confirmed 0.98 (edr)
- 2026-04-01T10:00:00Z · **N2** devuser applied a config from 192.0.2.7 — confirmed 0.95 (edr) · chain · legitimate activity · ran as devuser
- 2026-04-01T11:00:00Z · **N3** calendar entry deleted on host-b7 using j.doe's stolen session — confirmed 0.98 (edr) · chain · ran as j.doe, operated by intruder via the stolen session

## 3. Narrative

### N1 symptom

- Turn 1 (0.88): for, direct: direct supports

### N3 calendar entry deleted on host-b7 using j.doe's stolen session

- Turn 1 (0.98): for, direct: direct supports for, direct: direct supports
- artifacts: ip=192.0.2.7, ip=198.51.100.9, host=host-b7

### N2 devuser applied a config from 192.0.2.7

- Turn 1 (0.95): for, direct: direct supports for, corroborating: corroborating supports
- artifacts: user=devuser, ip=192.0.2.7

## Other findings (off the main chain)

- **N4** connection from 203.0.113.5 to host-c3 — confirmed 0.98 @ 2026-04-01T09:00:00Z (edr): direct supports

## Established by targeted searches

- (edr, found) j.doe's session was used from 198.51.100.9 only [r9]

## 4. Decisions


## 5. Raw layer

## 6. Open questions

- none
