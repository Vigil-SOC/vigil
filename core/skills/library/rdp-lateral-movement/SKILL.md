---
name: rdp-lateral-movement
description: Judge whether one internal RDP (Remote Desktop) session is lateral movement or an administrator doing their job. Read this for a finding on port 3389 or technique T1021.001 before deciding lateral-movement, suspicious, or expected-admin.
metadata:
  vigil-origin: "epic #882 slice 2, #1075"
---

# RDP lateral movement

Use this for one internal Remote Desktop session. It does not cover SMB, WinRM, or SSH. When the finding is not RDP, say so and stop. When it is RDP, end the turn with the labelled block. A lookup you cannot run is not a reason to stop early.

## Procedure

1. **Extract the session.** From `raw_data` take `src_host`, `dst_host`, the account (`src_user`, otherwise `user`, otherwise `account`), the timestamp, and any auth detail (`logon_type`). Keep the original spelling for the path line. If you only hold an id and you hold `get_finding`, call it and extract from the result. If you already hold the finding, do not call it.

2. **Ask what separates movement from administration.**
   - Is the account expected on both hosts?
   - Is the source a workstation or a jump host?
   - Is the destination a server that source should reach, or another workstation?
   - Is the hour normal for that account?
   - Did a credential-related finding on the source precede this session?
   - Is there a fan-out to further hosts?

   Answer from the finding first. If you hold `list_findings` or `search_findings`, search by the source host and the account. If you hold `recall_entity`, call it on the source host, the destination host, and the account. If none of these is callable, do not announce a lookup and do not invent a result. Never end a turn on an announced lookup.

3. **Read roles, hour, and prior compromise off the text.** Lowercase only while testing; echo the original spelling on the path line.
   - A host name containing `workstation` is a workstation, `jump` is a jump host, and `server` is a server. `fileserver` and `appserver` are servers.
   - An account starting with `svc-` is a service account. An account containing `admin` and not starting with `svc-` is a named admin account.
   - Read the hour digits from the timestamp as written. Hours 08 through 17 are business hours. Any other hour is off-hours. Do not convert timezones.
   - Prior compromise is stated in this finding when `description` or a `raw_data` string asserts that credential dumping, credential theft, or a compromise already happened. The title does not count.
   - A new account-host pair, a fan-out, or a logon-type mismatch counts only when the finding states it or a lookup returned it. A present `logon_type` other than 10 is a mismatch. Unknown is not a tell.
   - Benign patterns, until a tell breaks them: help-desk RDP, a scheduled admin window, an RMM tool. Tells that break them: workstation-to-workstation, a stated new account-host pair, a logon-type mismatch, and sessions chained within minutes of a credential-related event.

4. **Pick the verdict.** First match wins. Do not override a match. The finding title is not evidence.
   - `lateral-movement` when the source and the destination are both workstations, the hour is off-hours, the account is a service account, and prior compromise is stated in this finding or a lookup returned one.
   - `expected-admin` when the source is a jump host, the destination is a server, the account is a named admin account, the hour is business hours, and prior compromise is not stated in this finding and no lookup returned one.
   - `suspicious` for every other RDP session.

5. **Fill prior-compromise.** First match wins.
   - `stated in this finding` when this finding states prior compromise, including when no lookup ran.
   - the other finding's id when a lookup returned one and this finding does not itself state it.
   - `none found` only after a lookup returned nothing.
   - `not checked` when no lookup ran and this finding does not state prior compromise.

## Output

End with this block. Plain text, six lines, labels lowercase, one space after each colon. No bold, quotes, bullets, angle brackets, or pipe characters. Each angle-bracket phrase in the layout is a blank: replace it with one value and do not print the blank. The technique line is fixed text; copy it character for character. Between the two host names write a space, a hyphen-minus, a greater-than, and a space. A Unicode arrow is wrong.

`confidence` is `high` when every field of the matched rule is present in the finding, `medium` for `suspicious`, and `low` when a host, the account, or the timestamp is missing. `next` is `contain` for lateral-movement, `investigate` for suspicious, and `dismiss` for expected-admin. Use `monitor` only when the finding asks to watch and neither contain nor dismiss applies.

```
verdict: <lateral-movement|suspicious|expected-admin>
path: <src> -> <dst> as <account>
technique: T1021.001
prior-compromise: <finding id | stated in this finding | none found | not checked>
confidence: <high|medium|low>
next: <contain|investigate|monitor|dismiss>
```
