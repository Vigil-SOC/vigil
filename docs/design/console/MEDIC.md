# Medic console: UX requirements

## 1. Purpose and scope

This file holds the UX requirements for Medic in the console, UX-1 to UX-30: the Medic card on Health, the Medic fact in the shell status line, the desktop notification, and the `/medic` screen with its Incidents, Can't see, Content pack and Export tabs. Its readers are the implementers of the screen issues (#2101, #2103, #2105, #2107, #2109, #2113) and their reviewers; each of those issues cites the UX rows it builds (section 9), and where an issue's text and this file differ, this file wins. It is the Medic console's requirements, not phase-1 PRD scope (`PRD-phase1.md` does not cover Medic).

Medic's own contracts are the source for every state value, record type and field name used here: `services/medic/contracts/decision-record.schema.json` (the records Medic stores), `services/medic/contracts/medic-api.openapi.yaml` (the nine read and feedback operations, each naming its permission in `x-medic-permission`), `services/medic/contracts/pack-manifest.schema.json` and `services/medic/contracts/evidence-report.schema.json`. The console reaches Medic only through the backend's `/api/medic/…` routes, added by #2099, which check `medic.read` or `medic.admin` (`core/auth/permissions.py`; seeded by `infra/database/init/45_medic_permissions.sql`: Admin gets both, Manager gets `medic.read`).

### Overview

Medic shows up in three places. **(1)** A **status card on Health** shows Medic's single worst-wins status. **(2)** A **Medic fact in the shell status line** turns red for Down, Blind, Not recording and Crash-looping, backed by a desktop notification. **(3)** A **`/medic` screen**, gated by `medic.read`, has four tabs: **Incidents** (a list, then a detail with the "would have" panel, timeline, evidence and feedback), **Can't see** (sensors), **Content pack** and **Export**. Every write action needs `medic.admin`: rating, pack import, revert and export. Medic in observe-only mode asks the admin for nothing. Ratings are invited but never nagged, and the unrated state is always visible, because a missing rating is left out of Medic's evaluation metrics and never counted as "agree". Everything Medic stores is displayed as **text, never HTML** (section 10).

### Design sources

There is no Claude Design board for Health or `/medic`: both are More-menu screens, which `PRD-phase1.md` gives tokens only. Each surface below names the tokens and components it uses, on its own two lines. Tokens come from `tokens/tokens.json`; layout follows `DESIGN.md` §7 (Layout), including its Screen rules (check at 1440 px and 1920 px in a real browser, nothing sideways, prose wraps then stops at two lines, rows stay even, the heading matches the nav label, uniform tab strips, one accent); the visual system is `DESIGN.md` §6 (surfaces, type, controls, Good/Fair/Poor levels, motion). Reuse `EmptyState` (`clients/web/src/shared/ui.tsx`), `LevelBadge` (`clients/web/src/shared/LevelBadge.tsx`) and `InfoTip` (`clients/web/src/shared/InfoTip.tsx`) rather than writing new ones. Times are UTC, 24-hour, as `DESIGN.md` §2 says.

## 2. Surfaces and permissions

| ID | Surface | Who sees it | Content |
|---|---|---|---|
| UX-1 | Health card "Medic" | Anyone who can open Health. **The card fetches details only with `medic.read`**; without it, the card shows the status word alone, with no counts or links | The status card of section 3 |
| UX-2 | Shell status line | Everyone (status word only) | `Medic: <state> · <for N min>` |
| UX-3 | Desktop notification | Users with `medic.read`, a console tab open, and notifications on | Down, Blind, Not recording, Crash-looping |
| UX-4 | `/medic` (More menu, label "Medic") | `medic.read` | Tabs: `?tab=incidents` (default) · `cant-see` · `pack` · `export` |
| UX-5 | Write actions | `medic.admin` only; buttons are **hidden** without it, and the backend enforces it server-side (#2099) | Rate, comment, import, revert, switch to bundled, export |

The status word for everyone (UX-1 without `medic.read`, UX-2) comes from the `medic: {state, since}` field that #2099 adds to `/api/health`, the payload the status line already polls. There is no second, ungated Medic route. `/medic` is a new screen key, gated through `SCREEN_PERMS` in `clients/web/src/shell/SocConsole.tsx` and listed in `MORE_KEYS`; both entries are added by #2103, in the new folder `clients/web/src/screens/medic/` (new in #2103).

## 3. Status (Health card, status line, notification)

```
┌ Medic ─────────────────────────────── [Down] ┐   status word + colour (LevelBadge)
│ Last seen 14:02 (for 6 min) · heartbeat 14:01│   ⓘ InfoTip: how the status was derived
│ Up 3 d 4 h · 2 restarts in 24 h (exit: OOM)  │
│ Store: ok · 212 MB of 1 GiB                  │   store health block
│ Sensors: 11 reporting · 1 Medic can't see ›  │   link → /medic?tab=cant-see
│ Pack medic-core 0.4.1 stable · expires 41 d  │   link → /medic?tab=pack
└──────────────────────────────────────────────┘
```

The card's lines come from Medic's `Status` (`GET /v1/status` in `services/medic/contracts/medic-api.openapi.yaml`, through #2099): `heartbeat_at`, `uptime_s`, `restarts_24h`, `last_exit.reason`, `store.states`, `store.bytes_used` of `store.cap_bytes`, `sensors.reporting` and `sensors.cant_see`, and `pack` (`id`, `version`, `channel`, `expires_at`). "Last seen" is the backend's `last_seen_at` (`core/platform/medic_last_seen.py`); the status route that returns it to the console is added by #2099. When Medic is Off, Down or Unknown, only the lines the backend knows are shown; the others are left out, never shown as zero.

Tokens: `bg1` `ln0` `bg2` `tx0` `tx2` `good` `fair` `poor` `poor-bg` `fair-bg` `bg3` `ac`
Components: `card` `LevelBadge` `InfoTip` `NotMeasured` `MedicStatusCard` (new in #2101)

The status word is shown in its level's colour with `LevelBadge` (`variant="pill"`). Today `LevelBadge` can only print Good, Fair or Poor, so #2101 gives it an optional `label` prop that replaces the word and keeps the level's colour and glyph. Level per state: Running `good`; Degraded, Starting and Unknown `fair`; Down, Blind, Not recording and Crash-looping `poor`. Off has no level: it is the plain word "Off" in `tx2` on `bg3`, not a pill.

- **UX-6 States and weight.** Nine states, worst first. The backend decides three of them (`core/platform/medic_status.py`, `MedicStatus`): `off` (the `VIGIL_MEDIC_ENABLED` switch is off), `down` and `unknown`. When Medic answers, its own word applies, worst first, as `medic_state` in `services/medic/api/status.py` orders them: `crash_looping`, `not_recording`, `starting`, `blind`, `degraded`, `running`. The words shown are Off, Running, Down, Unknown, Starting, Degraded, Blind, Not recording and Crash-looping. **Blind uses the same red as Down** (`poor`). Off is grey ("Medic is off", `tx2` on `bg3`), not an error. Unknown reads "Unknown: Vigil can't tell (database unavailable)", never "Running". Running uses `good`. State is never conveyed by colour alone: the word is always shown.
- **UX-7 Status line.** Add one fact to the fold in `clients/web/src/shell/statusLine.ts` (`foldStatus`), rated **Poor** for Down, Blind, Not recording and Crash-looping, and **Fair** for Degraded, Starting and Unknown; Running and Off add no fact. The fact must refresh every 30 s. On main today the shell already polls the fold's reads every 30 s (`setInterval(pollStatus, 30_000)` in `clients/web/src/shell/SocConsole.tsx`), and `/api/health` is one of those reads, so the fact reads the `medic` field of `/api/health` (added by #2099) with no new request. A health payload with no `medic` field adds no fact: that build is not reporting Medic, and it is not "status unavailable". Keep the last good value while a round runs; if the health read itself fails, show "Medic: status unavailable" (Fair), never the old green.
- **UX-8 Notification.** Use `notificationService` (`clients/web/src/services/notifications.ts`, its `show` method) with a stable `tag` (`medic-status`), so a state repeats only once, and `requireInteraction` for Down and Blind. Notify only on entering a red state (Down, Blind, Not recording, Crash-looping), plus once on recovery ("Medic is running again"). It follows the user's existing notification setting (`clients/web/src/shell/useDesktopNotifications.ts`).
- **UX-9 Dev mode.** When Medic reports dev mode (`dev_mode: true` in `Status`), show a permanent amber strip on the card and on `/medic`: "Dev mode: unsigned content allowed; results don't count toward the gate."

Tokens: `fair` `fair-bg` `fair-ln`

## 4. Incidents tab

```
Incidents   [Open ▾] [All lanes ▾] [Unrated ☐]       Rated 7 of 19 routed incidents (37%) ⓘ
┌──────────┬──────┬──────────────────────────────────┬────────┬──────────┬───────────┐
│ State    │ Lane │ What                             │ Opened │ Duration │ Rating    │
├──────────┼──────┼──────────────────────────────────┼────────┼──────────┼───────────┤
│ Firing   │ 2    │ LLM provider key rejected        │ 13:41  │ 27 min   │ Unrated   │
│   └ 2 symptoms suppressed by this ›                                                  │
│ Flapping │ 3    │ Federation source stale (splunk) │ 09:10  │ reopened │ Agree     │
│          │      │                                  │        │ ×3       │           │
│ Held     │ 1    │ Worker queue not draining        │ 13:58  │ upgrade  │ —         │
│          │      │ until 14:13                      │        │          │           │
│ Can't    │ 3    │ Medic can't see: daemon /status  │ 12:30  │ 1 h 38 m │ —         │
│ evaluate │      │                                  │        │          │           │
└──────────┴──────┴──────────────────────────────────┴────────┴──────────┴───────────┘
```

The list is `GET /v1/incidents` (`listIncidents`), one `IncidentSummary` per row, with the `state` (`open`, `resolved`, `all`), `lane`, `subject` and `unrated` filters and cursor pagination (`next_cursor`). An incident here is Medic's record (`incident_id` on `/v1/incidents`), not a Case: `DESIGN.md` §2 reserves Case for the SOC unit of work, and this screen does not use that word.

Tokens: `bg0` `bg1` `bg2` `bg3` `ln0` `ln1` `tx0` `tx1` `tx2` `ac` `poor` `fair`
Components: `TabStrip` `DataTable` `FilterChip` `InfoTip` `NotMeasured` `EmptyState` `tab-strip` `row-stack` `MedicIncidentRow` (new in #2103)

- **UX-10 Row = one incident** (rule × group, `incident_id`). "What" is the rule's pack title (`title`) plus up to 3 `group` values, all as text.
- **UX-11 State words**, from the incident's records and summary: Firing (`state: firing`) · Resolving (`resolving`) · Resolved, with how (`resolved_how`: `cleared`, `rule_retired`, `group_retired`) · **Flapping** · **Held** · **Can't evaluate** (`last_eval: unknown`; never shown as healthy) · Closed quietly (`resolved_how: closed_quietly`, a suppressed symptom that resolved without routing). When `reopen_count` is above 0 the state column says Flapping instead of Firing or Resolving, and Duration says "reopened ×N"; Resolved, Held and Can't evaluate keep their own words. Held has two shapes. An upgrade hold (`held_until` set, the opened record's `held_by_upgrade_until`) reads "Held: upgrade in progress until HH:MM". A suppression child still in its 5-minute routing hold (`state: held` and `held_until` null; `incident_opened.route` is `held`) reads "Held", with no until-time and not the word "upgrade". That hold is not yet the nested row in UX-12 (`state: suppressed`). Watcher incidents (`subject: watcher`) read **"Medic can't see: <sensor>"**.
- **UX-12 Suppression.** A suppressed child (`state: suppressed`, `suppressed_by` its parent) is nested under its parent ("N symptoms suppressed by this", from the parent's `suppressed_children.count`), not listed as its own row or counted as an alarm. If it later routes (an `incident_updated` record with `change: unsuppressed`), it becomes a normal row.
- **UX-13 Unrated is visible.** There's a rating column, an "Unrated" filter (`unrated=true`), and a header showing the response rate over routed, rateable incidents (UX-21): "Rated X of Y" from `counts.rated` and `counts.rateable`. No badges, pop-ups or notifications ask for ratings.
- **UX-14 Freshness.** Poll every 30 s while the tab is visible; keep the last good list. If a fetch fails, show "Last updated N min ago". Paginate with the API's cursor.

## 5. Incident detail

```
◀ Incidents   LLM provider key rejected · lane 2 · Firing 27 min · rule llm.key-rejected r3 · pack 0.4.1
┌ What Medic would have done ───────────────────────────────────────────────┐
│ Observe only (L0): recorded this incident.                                │
│ Recommend (L1): tell the admin: "Rotate the Anthropic key in Settings ›   │
│   LLM providers; the current key is rejected (401)."                      │
│ Autonomous (L2): the same notice. Lane 2 is yours to fix, so nothing runs │
│ Nothing was done. Medic is in observe-only mode.                          │
└───────────────────────────────────────────────────────────────────────────┘
Timeline                                          Evidence (3)
13:39  Condition first true (active since)         ▸ llm-worker log · 13:39:12 · ERROR
13:41  Opened · lane 2 (rule lane)                   "provider returned 401 for key ***"
13:41  Routed                                        redaction v1 · obs 7f3a…
13:44  Symptom "worker queue not draining"         ▸ bifrost routability · key k2 · invalid
       suppressed by this                          ▸ …
13:52  Resolving (condition false)
13:53  Firing again (condition true)
14:05  admin: Agree
┌ Your rating ──────────────────────────────────────────────────────────────┐
│ ( Agree ) ( Fix is wrong ) ( Wrong lane ▾ 1 2 3 ) ( False alarm )         │
│ Comment (optional, 0/500). Stays on this install, redacted before saving  │
│ [                                                              ] [Save]   │
└───────────────────────────────────────────────────────────────────────────┘
```

The detail is `GET /v1/incidents/{incident_id}` (`getIncident`: the summary plus the `incident_opened` record exactly as stored); the timeline's records are `GET /v1/decisions` filtered by `incident_id` (`listDecisions`).

Tokens: `bg1` `bg2` `ln0` `tx0` `tx1` `tx2` `vio` `vio-bg`
Components: `card` `PageHead` `InfoTip` `MedicWouldHave` `MedicTimeline` `MedicEvidence` (new in #2103)

- **UX-15 "Would have" panel.** Show the opened record's `would_have` for L0, L1 and L2 in plain words. L0 is always `record`: "recorded this incident". An action reads: `notify_admin` "tell the admin", then `advice.fix` when the record has advice; `propose_runbook` "propose the runbook"; `run_runbook` "run the runbook"; `prepare_support_bundle` "would have prepared a support bundle for DeepTempo Support". L2's `not_eligible` qualifies its line, and these are the only three values (`services/medic/contracts/lane_ref.py` `would_have`, `decision-record.md` R8). `not_lane1` on lane 2: L2 repeats L1's words and adds "Lane 2 is yours to fix, so nothing runs". Lane 3's L1 and L2 are both `prepare_support_bundle` (L2 carries `not_lane1`): that is the support-bundle sentence above, and it does not add a second "nothing runs". `no_runbook`: L2 would only have notified the admin — "No runbook, so nothing runs." `runtime_untrusted`: "L2 would only have proposed this, because a value came from untrusted input". If the incident has an `incident_updated` record with `change: escalation_simulated`: "Still open after N fix attempts: would have escalated to DeepTempo Support (lane unchanged)", where N is that record's `simulated_failures`. Always end with "Nothing was done. Medic is in observe-only mode."
- **UX-16 Timeline** renders every record for the incident in `seq` order: opened (`incident_opened`, plus `active_since` as its own earlier row), routed or held (`route`, or `change: routed`), suppressed or unsuppressed (linked to the other incident), resolving or refiring (with `eval`; **`unknown` reads "Medic couldn't evaluate"**), reopened (flapping), escalation simulated (`change: escalation_simulated`), evidence added (`change: evidence_added`), resolved (`incident_resolved` with `how`), and `feedback` and `adjudication` records. An adjudication row is labelled as the evaluation's verdict (`verdict`, `basis`), never as an admin rating. Times come from the record's `at`.
- **UX-17 Evidence is text only.** Excerpts (`excerpt.text`) render in a `<pre>` as React text children. **No `Markdown`, no `dangerouslySetInnerHTML`, no auto-linking.** ANSI escapes are stripped for display, and control characters are shown as `\x1b`-style escapes. Each item shows its source (`signal`), time (`t`), level, observation id (`observation_id`) and `redaction_version`, plus `key` and `value` when the item carries them. The evidence item in `services/medic/contracts/decision-record.schema.json` (`$defs.evidence`) has no level field on main; until one is added, the level is left out, and it is never parsed out of `excerpt.text`. Pack advice (`advice.summary`, `advice.fix`) is static pack text, rendered the same way (plain text, line breaks kept).
- **UX-18 Tests.** Fixtures in evidence, advice, group values and comments: `<img src=x onerror=alert(1)>`, a Markdown link to a script URL (the string `[x]` immediately followed by `(javascript:alert(1))`), `javascript:` URLs, ANSI colour and cursor codes, 10 KB strings, RTL override characters. Each must render inert, as visible text.

## 6. Feedback

The feedback set is the record's `value`: `agree`, `disagree`, `wrong_lane` (with an optional `suggested_lane`) and `false_alarm`, with `source` `live` or `replay` and an optional comment. It is not "agree, wrong lane, false alarm, comment". Saving is `POST /v1/incidents/{incident_id}/feedback` (`postFeedback`) through #2099, with the console's existing CSRF protection; the incident's `allowed_feedback` lists exactly the values it accepts now.

| Button | Stored as | Shown when | Means |
|---|---|---|---|
| **Agree** | `agree` | always | The diagnosis and proposed action are right |
| **Fix is wrong** | `disagree` | the incident carries fix text (`has_fix`: lane 2, or lane 1 with a runbook) | Real problem, wrong advice |
| **Wrong lane** ▾ 1/2/3 | `wrong_lane` + optional `suggested_lane` | Vigil incidents (`subject: vigil`) | Real problem, wrong owner |
| **False alarm** | `false_alarm` | always | Nothing was wrong |

"Fix is wrong" appears only with fix text, because there is nothing to disagree with on an incident without one. Watcher incidents get Agree and False alarm only.

Tokens: `bg2` `ln1` `tx0` `tx2` `ac` `ac-bg` `poor`
Components: `TextInput` `EmptyState` `MedicRating` (new in #2105)

- **UX-19 One current rating per admin per incident.** Changing it appends a new `feedback` record (records are append-only); the latest one is "your rating" (from the incident's `ratings`, matched to the session by the backend), and earlier ones stay in the timeline, so a mis-click can be corrected and a second admin never silently overrides the first. Ratings are allowed while open or after resolve. The `source` is `live`. **The admin identity comes from the session (#2099), and the request carries no user field.**
- **UX-20 Comment:** optional, ≤ 500 characters with a live counter. The label says it stays on this install and is redacted before saving. After saving, show the **stored (redacted)** text returned in the saved record (`comment.text`), so the admin sees what was kept. Comments are visible to `medic.read`, and never exported.
- **UX-21 Rateable incidents:** routed Vigil incidents, plus watcher incidents (Agree and False alarm only). **Suppressed symptoms aren't rateable** while suppressed; the parent's rating covers them, so one outage isn't counted twice. "Closed quietly" incidents aren't rateable. Held incidents become rateable once routed. The incident's `rateable` and `allowed_feedback` carry this; a refused rating comes back as `not_rateable` with a `reason` (`suppressed`, `closed_quietly`, `not_routed`, `value_not_offered`).
- **UX-22 Agreement denominator.** Medic's evaluation computes agreement as `agree ÷ (agree + disagree + wrong_lane)` over incidents with fix text. The UI doesn't show the agreement percentage; it shows only the response rate (UX-13), so admins aren't steered.
- **UX-23 Replay review session** (built only if the pilot's design partners agree to a replay session; not part of #2103). It's a separate tab, `/medic?tab=review`, entered from a button that only `medic.admin` sees. It has a full-width violet strip: "Review session: rating recorded corpus incidents, not this install's". It uses the same buttons, stored with `source: replay` (with the record's `replay_head`), and replay incidents never appear in the Incidents tab. The feedback request in `services/medic/contracts/medic-api.openapi.yaml` is live-only today; a replay path is added with this tab.

Tokens: `vio` `vio-bg`

## 7. Can't see, Content pack, Export

### Can't see (`?tab=cant-see`)

The list is `GET /v1/sensors` (`listSensors`), one `Sensor` per row.

Tokens: `bg1` `bg2` `ln0` `tx0` `tx2` `poor` `fair`
Components: `DataTable` `NotMeasured` `EmptyState` `row-stack`

- **UX-24** One row per sensor (its `id`, a signal id from `services/medic/contracts/signal_ids.json`): Reporting · **Medic can't see** · Off (`state`: `reporting`, `cant_see`, `off`), with since (`since`), last good reading (`last_good_at`), the reason enum (`reason`: `auth_401`, `auth_403`, `unreachable`, `timeout`, `http_5xx`, `parse_error`, `not_found`, `disabled_by_config`, `redis_fault`, `unknown`), and **dropped lines in the last hour** (`dropped_last_hour`). The enum is marked provisional on the contract; `unknown`, and any value the console does not know, reads "Unknown". A sensor that can't be read is never shown as 0 or green: it reads "Unknown" with `NotMeasured` styling, and a null `dropped_last_hour` is not measured, never 0. Link to the watcher incident (`watcher_incident_id`), if one is open.

### Content pack (`?tab=pack`)

The facts are `GET /v1/pack` (`getPack`, `PackState`): `active`, `previous`, `bundled`, `withdrawn`, `revoked_notice`, `trust_root` and `suppression_7d`.

Tokens: `bg1` `bg2` `ln0` `tx0` `tx2` `fair` `fair-bg` `poor` `poor-bg` `poor-ln`
Components: `card` `InfoTip` `ConfirmDialog` `HoldButton` `EmptyState` `hold-to-confirm` `MedicPackFacts` (new in #2107)

- **UX-25 Facts:** pack id, version, channel (`pack.channel`: `dev` or `stable`; the schema also allows `beta`), source (`source`: **bundled**, or **imported by <admin> on <date>** from `imported_by` and `activated_at`), created (`pack.created_at`), **expires in N days** (`pack.expires_at`; amber at ≤ 30, red "Expired, still running" after, when `stale` is true), Vigil range (`vigil.min` to `vigil.max_exclusive`), trust root version and expiry (`trust_root.version`, `trust_root.expires_at`). The previous pack (last-known-good) shows the same facts. Neither `PackFacts` nor the manifest has a `pack_diff` field, so the link is left out until one exists, the same way UX-17 leaves out a missing evidence level. #2107 does not invent the field. When a field does exist, that one link opens in a new tab with `rel="noopener noreferrer"`.
- **UX-26 Suppression counts:** "N incidents suppressed by rule X (last 7 d)", one row per suppression entry that fired (`suppression_7d`: `rule`, `suppressed`).
- **UX-27 Actions (`medic.admin`).** The import, revert and switch operations are added under `/v1/pack/` by #2026, and the console calls them through backend routes under the same rules as #2099 (explicit `medic.admin`, CSRF, session identity).
  - **Import pack:** one `.json` file in `medic.signed-pack/v1` form (a DSSE envelope signing a statement of the pack's sha256 and size, plus the pack itself). The pack is at most 4 MB and the whole file at most 4 MB + 64 KB (`MAX_IMPORT_BYTES` in `services/medic/contracts/trust_check.py`), and the console refuses a larger file before upload. The confirm step shows the new version against the active one. A refusal shows the code and the message the checker already returned with it: an `S-` pair from `services/medic/contracts/trust_check.py`, or a `P-` or `E-` pair from `services/medic/contracts/pack_check.py` and `services/medic/contracts/rule_check.py` (each error is `(code, message)`). There is no second list of reasons. The active pack stays.
  - **Revert to previous pack:** a confirm modal shows both versions and whether the target is stale or out of range (`stale`, `in_vigil_range`). Afterwards, the reverted-from version shows **"Withdrawn on this site"** (`withdrawn`), and re-importing it needs a second confirm (a logged override: a `pack_event` record with `event: override`).
  - **Switch to bundled:** shown only when the active pack is out of Vigil's range after an upgrade (`in_vigil_range: false`, with `bundled` set). Logged override.
- **UX-28 Revoked key:** a red strip says "Pack X was removed: its signing key was revoked. Medic is running the bundled baseline Y." (from `revoked_notice.removed` and `revoked_notice.running`). It stays until an admin acknowledges it, and the acknowledgement is logged; the acknowledge operation is added by #2026.

### Export (`?tab=export`)

The report is `GET /v1/export` (`getEvidenceExport`): one JSON document in the closed schema `services/medic/contracts/evidence-report.schema.json`, with its sha256 in the `X-Medic-Sha256` header.

Tokens: `bg1` `bg2` `ln0` `tx0` `tx2` `ac`
Components: `card` `DurationPicker` `EmptyState` `MedicExportPreview` (new in #2113)

- **UX-29** `medic.admin` only. Pick a period (defaults to the last 7 days; the API takes at most 31 days, not in the future). With `DurationPicker` the period is "the last N" ending now, clamped to 31 days. **Generate** produces the evidence report. The console then shows the **exact bytes** in a read-only monospace viewer, with size, sha256 and the chain head (`chain_head`), plus the line "Contains counts, rates, rule IDs and enums only. No logs, evidence or comments". **Download** saves the same bytes it previewed, with no second fetch. **There is no "send" button.** The admin sends the file through their own channel, so the console adds no path off the site. Each export is written to the backend's audit log (#2099).

## 8. Empty, error and loading states

- **UX-30 Empty, error and loading states.** Use `EmptyState` throughout (`loading` and `error` for the live region). **Off:** "Medic is off on this install." Admins also see "Set `VIGIL_MEDIC_ENABLED=true`" (see `docs/medic/enable.md`). **Down or Unknown:** `/medic` still shows the last stored incidents, with a red strip "Medic is down: showing data up to HH:MM". **No incidents:** "No incidents. Medic is watching 11 sensors." (the count is `sensors.reporting`). Never show "All healthy" while any sensor is in "can't see".

Tokens: `poor` `poor-bg` `tx2`
Components: `EmptyState`

## 9. Traceability

Which issue builds each requirement. Each screen issue cites its rows; "not assigned" means no open issue builds it yet.

| Section | Requirement | Built by |
|---|---|---|
| §2 | UX-1 Health card, status word for everyone | #2101 (the `medic` field in `/api/health`: #2099) |
| §2 | UX-2 Status line | #2101 |
| §2 | UX-3 Desktop notification | not assigned (#2103 leaves it out) |
| §2 | UX-4 `/medic` and its tabs | #2103 (Incidents), #2107 (Can't see, Content pack), #2113 (Export) |
| §2 | UX-5 Write actions for `medic.admin` only | #2105 (rate, comment), #2109 (import, revert, switch), #2113 (export); server side #2099 |
| §3 | UX-6 States and weight | #2101 |
| §3 | UX-7 Status line fact | #2101 |
| §3 | UX-8 Notification | not assigned (#2103 leaves it out) |
| §3 | UX-9 Dev mode strip | #2101 (card); #2103 (`/medic`) |
| §4 | UX-10 Row = one incident | #2103 |
| §4 | UX-11 State words | #2103 |
| §4 | UX-12 Suppression | #2103 |
| §4 | UX-13 Unrated is visible | #2103 |
| §4 | UX-14 Freshness | #2103 |
| §5 | UX-15 "Would have" panel | #2103 |
| §5 | UX-16 Timeline | #2103 |
| §5 | UX-17 Evidence is text only | #2103 (and every screen issue for its own strings) |
| §5 | UX-18 XSS fixtures | #2103, #2105 (comments), #2107 (sensor and pack text), #2109 (refusal text) |
| §6 | UX-19 One current rating per admin | #2105 |
| §6 | UX-20 Comment | #2105 |
| §6 | UX-21 Rateable incidents | #2105 |
| §6 | UX-22 Agreement denominator | #2105 (shows the response rate only) |
| §6 | UX-23 Replay review session | not assigned: filed as its own issue only if the pilot's design partners agree |
| §7 | UX-24 Can't see | #2107 |
| §7 | UX-25 Pack facts | #2107 |
| §7 | UX-26 Suppression counts | #2107 |
| §7 | UX-27 Pack actions | #2109 (Medic operations: #2026) |
| §7 | UX-28 Revoked key strip | #2107 (acknowledge operation: #2026) |
| §7 | UX-29 Export | #2113 (report: #2111) |
| §8 | UX-30 Empty, error and loading states | #2101 (status part), #2103; each tab's own states: #2107, #2113 |

## 10. Security notes for reviewers

Everything Medic stores came from Vigil's logs, a pack, or an admin, so the console treats all of it as untrusted text. These are requirements for every screen issue, and a reviewer checks each one:

- **Text, never HTML.** Every Medic string (evidence excerpts, pack advice and titles, group values, sensor reasons, pack facts, comments) renders as React text children. Evidence and advice render in a `<pre>`. No `Markdown` (`clients/web/src/shared/Markdown.tsx` is not used on `/medic`), no `dangerouslySetInnerHTML`, no auto-linking (UX-17).
- **Escapes.** ANSI escapes are stripped for display; other control characters are shown as `\x1b`-style escapes, so they can't move the cursor or hide text (UX-17).
- **Fixtures.** Each screen's tests feed these fixtures into every Medic string it shows, and each must render inert, as visible text: `<img src=x onerror=alert(1)>`, a Markdown link to a script URL (the string `[x]` immediately followed by `(javascript:alert(1))`), a `javascript:` URL, ANSI colour and cursor codes, a 10 KB string, and RTL override characters (UX-18).
- **The one external link**, once a `pack_diff` field exists, is that link: new tab, `rel="noopener noreferrer"`. Until then `/medic` has none (UX-25).
- **Identity from the session.** The feedback request carries no user field; the backend sets the admin from the session (UX-19).
- **No path off the site.** Export is download only, with no send button (UX-29).
- **Hidden and enforced.** Write controls are hidden without `medic.admin`, and the backend refuses them anyway (UX-5).
