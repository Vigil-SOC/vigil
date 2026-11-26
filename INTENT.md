---
# Declared daemon intent: what the SOC daemon may do unattended.
#
# Every key here is an existing DaemonConfig knob; there is no key without a
# reader. Observe mode: at startup the daemon logs each key whose declared
# value differs from the effective one (declared, effective, source, and
# whether applying the declared value would tighten or loosen autonomy).
# Nothing is enforced and nothing is written. Check from a shell with
#   python -m services.daemon.intent
# Values below equal the shipped defaults, so a fresh checkout reports none.

triage:
  # Run AI triage on new findings without being asked. (DAEMON_AUTO_TRIAGE)
  auto_triage: true

enrich:
  # Enrich findings with threat intel automatically. (DAEMON_AUTO_ENRICH)
  auto_enrich: true

respond:
  # Let the daemon execute response actions on its own. (DAEMON_AUTO_RESPONSE)
  auto_response: true
  # Minimum triage confidence for an unattended response; higher is tighter.
  # (DAEMON_CONFIDENCE_THRESHOLD)
  confidence_threshold: 0.90
  # Offer a finding to AutoOps intake at or above this; higher is tighter.
  # (DAEMON_REVIEW_THRESHOLD)
  review_threshold: 0.85
  # Below this, keep monitoring rather than act. (DAEMON_MONITOR_THRESHOLD)
  monitor_threshold: 0.70

act:
  # Unattended reversible outside action. Off until a person raises it.
  # DAEMON_FORCE_APPROVAL and DAEMON_AUTO_RESPONSE can force this off; neither
  # can turn it on. (autonomy.ceiling act_reversible)
  reversible: false
  # Unattended irreversible outside action. Same floor. Not a grant.
  irreversible: false
  # Two-person rule. Stored on the ceiling and not enforced.
  four_eyes: false
  # Hold a model's act on a declared crown jewel for a person. On is tighter; no
  # env setting moves it, and a declared false waits for a person's accept.
  hold_crown_jewels: true

investigate:
  # Autonomous investigation orchestrator. (ORCHESTRATOR_ENABLED, or the
  # Settings UI, which writes the orchestrator.settings overlay)
  enabled: false
  # Budgets per investigation and per hour; lower is tighter.
  max_cost_per_investigation: 5.0   # USD (ORCHESTRATOR_MAX_COST)
  max_total_hourly_cost: 20.0       # USD (ORCHESTRATOR_MAX_HOURLY_COST)
  max_runtime_per_investigation: 3600  # seconds (ORCHESTRATOR_MAX_RUNTIME)

# Each investigate workflow's own tier, keyed by workflow id. Applied only from
# a declared directory (VIGIL_INTENT_DIR); observe mode ignores this section.
# tier is act or assist; floor is a whole number 1..100, required with act and
# refused with assist. Assist and a higher floor are tighter. A workflow not
# listed follows the console as today. For example:
#   workflows:
#     full-investigation: {tier: act, floor: 85}
#     triage-deep-dive: {tier: assist}
workflows: {}
---

# Daemon intent

This file records what the Vigil daemon is meant to do without a human in the
loop. The frontmatter above is the machine-readable part; this body is for the
operator and is not parsed.

## Why a file

The knobs above already exist as typed environment variables, as the autonomy
ceiling and other overlays the Settings UI writes to the database, and as
defaults in code. The act flags are that ceiling. An environment variable can
force them off and cannot turn one on. None of those has
a history a reviewer can read: a change to `DAEMON_CONFIDENCE_THRESHOLD` leaves
no trace of who made it or why. This file lives in the repository, so a change
to declared intent is a commit with an author, a message and a review.

## What the daemon does with it

Observe mode only. At startup the daemon reads this file and, for every key
whose declared value differs from the value it is actually running with, logs
one line: the declared value, the effective value, where the effective value
came from (`env`, `db` or `default`), and whether applying the declared value
would `tighten` or `loosen` autonomy. It then runs exactly as it would have
without the file. A missing or unreadable file is one warning.

`python -m services.daemon.intent` prints the same table without starting the
daemon. Override the path with `VIGIL_INTENT_PATH`.

`python -m services.daemon.intent --replay` re-decides every finding that has
a triage confidence and every approval action in a window (default 7 days,
`--since 7d`) under this file and under the effective config, and prints the
rows whose outcome would differ. It does not write.

## Enforced: a declared directory

Set `VIGIL_INTENT_DIR` on the daemon to a directory holding an `INTENT.md` like
this one and a `REVISION` file whose first line names the revision (a plain
checkout writes it in a post-merge hook: `git rev-parse HEAD > REVISION`). The
daemon re-reads both every 30 seconds and applies a revision when they change:
keys whose declared value changed and that tighten autonomy are written to the
store each already has (the autonomy ceiling, the orchestrator settings, or
the `intent.applied` row for triage, enrich and respond), and every process
reads them within seconds. A loosening is not written; it is recorded as
pending for a person to accept. A file that does not parse, an unknown key or
a wrong-typed value rejects the whole revision. A poll that brings nothing new
writes nothing, so a setting narrowed from the console stays narrowed until a
commit changes that key. This file at the repo root is never enforced.

Settings → Limits & autonomy → Declared intent, `GET /api/config/intent` and the /mcp
tool `get_intent_report` show each managed key with its file, line and
revision, whether it has drifted (narrowed locally or by an env lock), whether
a loosening is pending, and an env value the declaration overrides. A local
change that narrows a managed key is stored; one that widens it is refused
with 409 naming the file and revision. Setting the declared value back is the
reset. A person with `ai_autonomy.write`, never a service account or API
token, accepts pending loosenings with `POST /api/config/intent/accept`
(`{"revision": ...}`); each store write is audited under their name with the
revision.

The `workflows` section sets each listed investigate workflow's tier and
floor, as Limits & autonomy does. Assist, or a higher floor on an Act
workflow, is written on the next poll; Act, or a lower floor, is pending until
a person accepts it, and the accept stores the investigation model in force
and its 30-day record, as a console raise does. A console change that would
let a managed workflow act where the declared entry would not is refused with
409; one that narrows it is stored and reported as drift ("narrowed locally"),
and so is Vigil's own lowering of it, the floor drop or a model change
("lowered by Vigil"). Raising it in the console to the declared value is the
reset. An id this install does not have, or one that is not an investigate
workflow, is listed as skipped with its line and the rest of the revision
applies; a missing `tier`, a `floor` that is not a whole number from 1 to 100,
a `floor` on Assist or any other key in an entry rejects the revision.

A folder with no `INTENT.md` reads intent as `degraded` (the last applied
revision stays), and an `INTENT.md` whose front matter is empty declares
nothing, so no key is managed.

### Site context: `site/*.md`

Beside `INTENT.md`, a `site/` folder states what Vigil cannot discover about
the site. Each `site/*.md` is Markdown with YAML front matter; the body is
prose for people. Any file may hold any of four lists, and only these keys:

```yaml
---
zones:
  - name: core                      # unique across all files
    selectors: ["cidr:10.1.0.0/16"]
    description: Payment systems    # optional
crown_jewels:
  - name: payments-db
    mission: Take card payments
    selectors: ["host:db01.corp.example", "ip:10.1.2.3"]
    zone: core                      # optional; must be a declared zone
    why: Holds card data            # optional
owners:
  - selectors: ["cidr:10.1.0.0/16"]
    team: Payments                  # a Vigil team, matched ignoring case
blind_spots:
  - selectors: ["host:ot-*"]
    what: No EDR on the OT hosts
---
```

A selector is `cidr:<range>` (matches `ip:` keys) or an Entity Key of kind
`ip`, `domain`, `host`, `url`, `email`, `hash`, `arn`, `aws_key`, `user` or
`process`, normalised as Vigil mints keys, where `*` in the value matches any
run of characters. No other wildcard or regex. No key takes a secret.

The same 30-second poll applies `site/` on its own verdict, in its own
`site.applied` row, with or without `INTENT.md`; its hash covers `site/*.md`
in name order and `REVISION`. An unknown key, a wrong-typed value, any other
selector kind (`edr_group:` included), a crown jewel naming an undeclared zone,
or a revision over 512 KiB or 2000 items refuses the whole site revision with
file and line, and the last good one stays. A site refusal never holds up an
`INTENT.md` change, nor the reverse. A directory with no `site/` declares an
empty site; one that cannot be read keeps the last good revision, `degraded`.
An owner naming no active team reads `unassigned: no team named <name>` until
that team exists, with no new commit. With `VIGIL_INTENT_DIR` unset, the daemon
clears the site at start and leaves the intent row alone.

`GET /api/config/site` and the /mcp tool `get_site_context` (both
`platform.read`, not part of `/api/v1`) show the applied revision, its state,
any refusal, each item with file, line and resolved owner, and the files' text.

A declared crown jewel is left for a person. No model closes a case whose
findings name it, and a run no person started may not add a detection exception
whose `match` value names it or a range holding it, dismiss a finding that names
it, or make a reversible or irreversible outside call with an argument value
naming it (wildcard selectors do not count for outside calls). The act is held
with the crown jewel's name, file, line and revision; people's acts are
unchanged. The limits: a vendor device id is not read; a crown jewel declared
only by name does not match a range, so declare its addresses too; a case whose
findings name no crown jewel is not held; an exception keyed on no host,
address or account is not read.

An admin turns the hold off with `act.hold_crown_jewels: false`, the Settings
switch beside Four-eyes, or `PUT /api/v1/autonomy`. Turning it off needs
`ai_autonomy.write` and a person, and is refused while stopped or while the
declared directory declares it on; turning it on needs only `ai_autonomy.stop`.

## Editing

Change a value here when the intended autonomy changes, and say why in the
commit message. If the effective config disagrees after a restart, the log
tells you which of env, database or default is overriding the declaration.
