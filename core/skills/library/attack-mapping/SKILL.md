---
name: attack-mapping
description: Check or add ATT&CK technique ids on a finding by grounding each id in an observable. Read this when a triage, investigation, or correlation agent is checking or adding technique ids on a finding.
metadata:
  vigil-origin: "epic #882 slice 2, #1077"
---

# Attack mapping

Ground each technique id in an observable from this finding. The labelled block is the whole answer: write it first and always finish it, then at most two sentences. Keep every observable to a few words. Do not restate technique definitions. This turn may have no tools. If a tool is not callable, skip it and still write the block. Never end the turn on a lookup you announced.

## Observables

From `raw_data`, `description`, and `iocs`, note only fields that are present:

- process and command line (`process`, `command_line`)
- parent (`parent_process`)
- network destination and port (`dst_ip`, `dst_domain`, `dst_host`, `dst_port`)
- auth event (account, source address, `failed_attempts`, `time_window`)
- file written
- registry key
- email artefact (sender, subject, link, or attachment)

## Tools

Name a tool only when you hold it.

- If you hold `get_finding` and the turn has an id but not the finding, call it. If the finding is already in the turn, do not call it.
- If you hold `get_technique_rollup` and it is callable, call it once. `environment` is `seen` when that rollup's count for the technique id is greater than zero, and `not seen` otherwise.
- If you do not hold `get_technique_rollup`, or it is not callable, `environment` is `not checked`. Never guess a count.

## Status

Take `mitre_techniques` in listed order. An empty or missing list means there is nothing to confirm. For each id already on the finding:

- `confirmed` when an observable shows that technique's behaviour. An id already listed is never `added`.
- `wrong-sub-technique` when the behaviour is that family but the observable pins a different sub-technique.
- `unsupported` when no observable shows it.

Then add an id that is not on the list only when an observable shows the behaviour. Prefer the sub-technique when the observable pins it; use the parent only when it does not. An added id's status is `added`. Apply a pin only when this finding has its observables. Do not emit a technique from a pin that did not match, and do not add a second technique for the same fact.

Pins:

- `powershell.exe` or `pwsh`, with `-enc`, `-EncodedCommand`, or another encoded payload, pins PowerShell: T1059.001, tactic `execution`. Do not decode the payload and do not add a technique for anything inside it. The parent T1059 is the wrong sub-technique for that process. The same encoded command confirms T1027, tactic `defense-evasion`, when T1027 is already listed. Do not add T1027 unless the command line is encoded or obfuscated.
- Repeated failures against one account from one source pin password guessing, T1110.001, tactic `credential-access`. That is `failed_attempts` greater than one, a single account (`target_user` or `user`), and one source address. Failures spread across many accounts pin spraying, T1110.003. One account is not spraying and is not the parent T1110. Write T1110.001, not T1110.
- A file upload or large transfer to a web file-sharing host pins exfiltration over a web service, T1567.002, tactic `exfiltration`. A transfer site such as transfer.sh matches, as does a destination the description calls an upload to external cloud storage. Do not add the parent T1567, T1048, or a collection technique for that same upload. When that is the only confirmed or added technique, the tactics line is exactly `tactics: exfiltration`.

## Tactics and chain

Tactic names are lowercase ATT&CK form (`initial-access`, `execution`, `defense-evasion`, `credential-access`, `exfiltration`). A capitalised tactic name is wrong. The `tactics` line lists those tactic names, in the order of the `confirmed` or `added` technique lines, duplicates dropped, separated by a comma and a space. The first tactic is the tactic of the first confirmed or added technique, not that technique's id. `unsupported` and `wrong-sub-technique` add no tactic. One such technique means one tactic and no second word.

`chain` is `single-stage` unless this finding shows two ordered stages. Join stages with ` -> `. Do not invent an earlier stage from the tactic name.

`confidence` is `high` when each confirmed or added technique is pinned by a `raw_data` field, `medium` when only the description supports it, and `low` when the observable is ambiguous.

## Output

Plain text. One technique line per id: existing ids first, in listed order, then added ids. Labels lowercase, one space after each colon. No bold, bullets, quotes, backticks, or code fence. No angle brackets and no pipe-separated choices in the answer.

Inside a technique line the separator is the em dash from the layout below (—, U+2014), with one space on each side, twice: after the id and after the status. A hyphen-minus, an en dash, or `--` is wrong. Copy that dash from the layout. The status word is exactly `confirmed`, `added`, `unsupported`, or `wrong-sub-technique`.

```
technique: <id> — <confirmed|added|unsupported|wrong-sub-technique> — <observable>
tactics: <lowercase tactic of the first confirmed or added technique, then any others>
chain: <single-stage or tactic -> tactic>
confidence: <high|medium|low>
environment: <seen|not seen|not checked>
```
