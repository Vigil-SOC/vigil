# Emulators

An emulator is an outside tool that runs one ATT&CK technique on one host and
reports what it ran. A tool is an emulator because it is bound as one, never
because of what it calls itself: a person binds a tool as an emulator on its
row under **Settings → Limits & autonomy → Outside tools** (the select beside
its class, set to Emulator), as they bind any tool to a capability. Atomic Red
Team's `atomic_red_team_execute` ships bound as an emulator
(`core/integrations/bindings.yaml`); a person may unbind it there too.

The hosts every emulator may run on are listed under **Settings → Limits &
autonomy → Emulators**. Vigil refuses a call naming any other host before it
leaves, and the refusal names the listed hosts; with no host listed, every call
is refused.

The MITRE analyst holds every tool bound as an emulator, and Vigil scores what it ran in the
run's coverage report (`analyze_coverage` with the run's `run_id`). This page is
the shape a tool answers in for that to work.

## The call

A call that starts work takes:

| Field | Type | |
|---|---|---|
| `technique` | string | An ATT&CK technique id, such as `T1003` or `T1003.001`. Anything else is refused before it leaves. |
| `hostname` | string | The host to run on, as sensors report it. Must be a listed host. |
| `allowed_hosts` | list of strings | Optional. Declare it in your input schema and Vigil sends the listed hosts in it, so the tool can check them too. |

A tool whose work outlasts one call answers with a `handle`. A later call that
reads the rest takes:

| Field | Type | |
|---|---|---|
| `handle` | string | The handle an earlier answer gave. |

A call names a `hostname` or a `handle`; one with neither is refused. Vigil
never reads a handle for the analyst: the analyst calls again.

## The answer

An answer is either one bare step, or an object holding a list of them:

```json
{
  "steps": [
    {
      "id": "run-17/1",
      "technique_id": "T1059.001",
      "hostname": "lab-win-01",
      "started_at": "2026-11-24T14:02:11Z",
      "ended_at": "2026-11-24T14:02:19Z",
      "exit": 0
    }
  ],
  "handle": "run-17",
  "done": true
}
```

| Field | | |
|---|---|---|
| `steps` | list | The steps run so far. Omit it and the answer itself is read as one step. |
| `handle` | string | Optional. Where to read the rest. |
| `done` | boolean | With a handle: `false` while work is still running, `true` when the answer is complete. |

Each step is read for six fields only:

| Field | | |
|---|---|---|
| `technique_id` | string | An ATT&CK technique id. |
| `hostname` | string | The host the step ran on. |
| `started_at`, `ended_at` | ISO 8601 time, or epoch seconds or milliseconds | When it ran. One of the two is enough. |
| `exit` | integer or null | The step's exit code. Null or non-zero leaves the step unknown rather than missed. |
| `id` | string | Optional. A step reported again under the same id counts once. |

Anything else on a step or an answer — commands, output, logs — is evidence
for the analyst to read and is never scored.

## What the report sets aside

The coverage report scores each step once: by `id`, or else by technique, host
and start, keeping the latest report. It sets aside, with a reason and never
scored:

- a step with no `hostname` or no time, or whose `technique_id` is not an
  ATT&CK id;
- a step on a host that is not listed now. These hosts are also named in
  `outside_hosts`, and Vigil writes one audit-trail entry for each answer that
  names one.

Beside the techniques the report also says `capped` when an answer had more
rows than the call allowed, and lists in `unfinished` each handle whose latest
answer said `done: false`.

A step is joined to Findings on its hostname and its time window only.

## Changes

This shape changes only by addition: a field is never removed or given a new
meaning, so a tool that answers in it today keeps working.
