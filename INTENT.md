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
  # Confidence bands the responder decides on below that line; higher is tighter.
  # Queue for response / correlator "act" line. (DAEMON_REVIEW_THRESHOLD)
  review_threshold: 0.85
  # Below this, keep monitoring rather than act. (DAEMON_MONITOR_THRESHOLD)
  monitor_threshold: 0.70
  # Isolate a critical finding at or above this. (DAEMON_CRITICAL_ACTION_FLOOR)
  critical_action_floor: 0.70
  # Investigate a high finding at or above this. (DAEMON_HIGH_ACTION_FLOOR)
  high_action_floor: 0.80
  # Route every action through human approval regardless of confidence.
  # (DAEMON_FORCE_APPROVAL, or Settings -> Approvals in the UI)
  force_manual_approval: false

escalate:
  # Severities that page a human; a shorter list is tighter.
  # (DAEMON_ESCALATE_SEVERITIES)
  severities: [critical, high]

investigate:
  # Autonomous investigation orchestrator. (ORCHESTRATOR_ENABLED, or the
  # Settings UI, which writes the orchestrator.settings overlay)
  enabled: false
  # Budgets per investigation and per hour; lower is tighter.
  max_cost_per_investigation: 5.0   # USD (ORCHESTRATOR_MAX_COST)
  max_total_hourly_cost: 20.0       # USD (ORCHESTRATOR_MAX_HOURLY_COST)
  max_runtime_per_investigation: 3600  # seconds (ORCHESTRATOR_MAX_RUNTIME)
---

# Daemon intent

This file records what the Vigil daemon is meant to do without a human in the
loop. The frontmatter above is the machine-readable part; this body is for the
operator and is not parsed.

## Why a file

The knobs above already exist as typed environment variables, as overlays the
Settings UI writes to the database, and as defaults in code. None of those has
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

## Editing

Change a value here when the intended autonomy changes, and say why in the
commit message. If the effective config disagrees after a restart, the log
tells you which of env, database or default is overriding the declaration.
