# Decision record v1 and lane rules (G1 + G2)

**Status:** final, 2026-10-07. ⚑ calls decided by Craig (§8): all five as recommended. **Amended 2026-10-07 by S4b** (⚑ S4b-3 (a): evidence tie order, "signal" = rule signal). Aligned the same day with **E3 (Accepted)**, which defines incidents and suppression, including every field E3's note on the G2 page asks for (record `at` gives `opened_at`, `resolving_since`, `routed_at` and `resolved_at`). · **Contract:** `decision-record.schema.json`, `decision_chain.py`, `lane_ref.py`, `vectors/lanes/lanes-*.yaml` (10; integer-second format, its own folder since X1 #7), `fixtures/decision-records/` (3 valid chains covering all 8 record types, 16 invalid), `tests/test_decision_record.py`, `tests/test_lanes.py`, `tools/gen_g2_fixtures.py`. Test count: see `uv run pytest contracts/` (the whole suite; this file no longer pins a number). **Amended 2026-10-07 by S0** (X1 ⚑1–5, A3-5, S0 review): `pack_event`, derived ids, tick-stamped engine records, random `instance_id`.
**Builds on:** E2 (rule `lane`, `fault`, trust and lane-1 gate), E3 (one incident per rule × group; flapping reopen; suppression, 5-min routing hold, child-outlives-parent; built-in `watcher.sensor-blind`, lane 3), D3 (observation IDs, label rules), C2 (lane can depend on install shape), C4 (append-only S1/S2, sequence numbers, anchors, reset, sizing), K1 §6 row G2 and T-04/T-05/T-09/T-17, I1 and A4 (gate fields), Program Plan (L1 = admin clicks, L2 = watcher runs alone; 1C5 escalation after two failures). **Not yet available:** G4 (runbook descriptors), H2 (UX spec).

## 1. Decision in one paragraph

The decision log is **one append-only, hash-chained sequence of typed records per store**: incidents (opened, updated, resolved), admin feedback, adjudication, pack lifecycle events, purge anchors and store resets. Every leaf field is tagged with one of seven types, and a test fails on an untagged field. Free text is limited to **K2-redacted excerpts ≤ 500 chars** and **static advice copied from the signed pack**. An incident is E3's (one rule × group), and its **lane is its rule's lane**. Unknown signatures, and lane-1 rules that fail the gate at routing time, go to lane 3. **Causes beat symptoms through E3's suppression:** a symptom keeps its own incident and lane, carries `suppressed_by`, and never routes unless it outlives its cause. Each incident records what L0, L1 and L2 **would have done**, and Phase 0 simulates Phase 1's escalation (two failed fix attempts → lane 3) without changing the lane.

## 2. Lane and routing (G1)

| # | Rule |
|---|---|
| R1 | An incident's lane is its rule's `lane` (E2). The router never reads evidence text, group values or excerpts to pick a lane (K1 T-05). The lane is fixed at open, so `lane` in `incident_opened` is what the gate scores (I1, A4) |
| R2 | **Routing (E3 §6).** A rule that is no suppression child routes when it opens. A child holds 5 min; a parent open at any point in that hold suppresses it (`suppressed`, `suppressed_by`). A suppressed child never routes unless it is still open 10 min after its last parent resolves; then it routes on its own lane (`unsuppressed`, `routed`). Otherwise it closes quietly. Nothing routes inside an upgrade window (E3 §7; E3's vectors v17–v20 pin `routed_at`). This is how "the root cause's lane wins" (⚑1) is realised: only the cause routes |
| R3 | A lane-1 incident whose rule fails the E2 gate at routing time (untrusted, < 2 signals, no sample) goes to **lane 3** (`lane1_gate_failed`): a loaded pack can't produce it, so it signals a defect or tampering. A lane-1 incident whose *evaluation* read an untrusted value keeps lane 1, and L2 is capped at proposing (`runtime_untrusted`) (⚑2) |
| R4 | No rule claims it (`rule: null`) → **lane 3**, `unknown_signature`. E3 defines no catch-all in v1, so this is defensive |
| R5 | E3 flapping damping reopens an incident (`reopened`, `reopen_count`). The lane doesn't change |
| R6 | **Simulated escalation:** if L2 would have run a runbook and the incident is still open 2 × `verify_after_s` after routing, append `escalation_simulated` (2 failures). The lane doesn't change; it's a "would have" fact (⚑3) |
| R7 | **Watcher incidents** (`subject: watcher`; E3's `watcher.sensor-blind`, lane 3) route like any other, but the gate leaves them out of lane accuracy and the lane-3 case rate |
| R8 | **Would have:** lane 2 → notify the admin (L1, L2). Lane 3 → prepare a support bundle. Lane 1 → L1 proposes the runbook, L2 runs it; no runbook → notify the admin; runtime-untrusted → L2 only proposes |
| R9 | **Lane by install shape (C2):** when the same fault is lane 1 on one shape and lane 2 on another (Bifrost is bundled on Compose and host-native, external on Helm), the pack ships one rule per shape using E2's `shapes`. The router adds no shape logic. Placement *within* a shape (in-chart vs external Postgres or Redis on Helm) only matters for database faults, which are outside the v1 watch list; if a later class needs it, it's an E2 MINOR addition |

**Vectors** (`lanes-01`…`10`): single rule · lane 1 with runbook, with and without simulated escalation · runtime-untrusted lane 1 · gate failure at routing · unknown signature · suppressed by an open parent · routing hold (parent fires second) · child with no parent routes after the hold · child outlives its parent, and a sibling that closes quietly · watcher sensor-blind.

## 3. Record (G2)

Envelope: `v`, `seq` (strictly increasing, no gaps), `at`, `type`, `prev`, `hash`, `body`.

**Determinism (A3-5, decided 2026-10-07).** Replaying a recording must give a byte-identical log, so engine output carries nothing the replay can't reproduce:
- **`at`:** for `incident_opened`, `incident_updated` and `incident_resolved` it is the **evaluation tick T** (E3 §1, on the 15 s grid, whole seconds with no fraction: `$defs/ts_tick`; `active_since` too), never the wall clock. Every other type is not engine output and carries the wall clock when written. `at` is therefore not monotonic across types; `seq` is the order.
- **`incident_id`** = `decision_chain.incident_id(instance_id, rule id, group, active_since)`: `inc_` + 24 hex of sha256. It is **minted once, when the incident opens, and stored**; nothing recomputes it later. A flapping reopen (E3 §5) keeps the stored id even though the condition's new `active_since` differs. An unknown-signature incident (`rule: null`, G1 R4) is grouped by `fingerprint`, so two different unknown lines on one tick don't collide (S0 review R12). After a `store_reset`, a rebuilt incident with the same inputs gets the same id; that is the same incident, recorded in the new segment.
- **Replay writes to its own store** (E6), never the live one, starting at seq 0 with the recording's `instance_id`. **The replay store holds engine records only** (S0 review R4, decided 2026-10-07): no `pack_event`, feedback or adjudication. The pack and rules a replay ran are fixed at its start and named in the replay's manifest, outside the chain. **"Byte-identical" means the canonical bytes of the whole replay store** (every record, including `seq`, `prev` and `hash`).
- **Feedback on a replayed incident** goes in the **live** store with `source: replay` and `replay_head` = the replay store's head hash (required for replay, forbidden for live; S0 review R12, decided 2026-10-07), because replay ids equal live ids.

**`instance_id` (X1 ⚑5a).** `mi_` + 16 random hex, created at the watcher's first start and persisted **beside** the store (C4 volume, not inside the database), so a `store_reset` keeps it and records it. Never derived from a host or cluster name (it reaches H5 exports). One store has one `instance_id` (test).

**Group values (X1 ⚑1a).** `group[].value` is always label-safe: the engine applies D3's label rule at extraction, so a free-text arg such as "Azure Sentinel" is stored as `h_` + 16 hex, and the raw text appears only in a redacted evidence excerpt.

**Built-in rules (X1 #11).** `watcher.*` rules aren't pack content (`rule.schema.json` excludes them). Their records name `pack_id: medic-builtin` and `pack_version` = the engine API version that defines them (`1.0`).

**`eval` (X1 #8).** The engine's three values map to the strings `"true"`, `"false"` and `"unknown"`; vectors write the first two as YAML booleans.

| `type` | Body (key fields) | Who writes |
|---|---|---|
| `incident_opened` | `incident_id`, `subject` (vigil \| watcher), `instance_id`, `install_shape`, `rule` {id, revision, pack id and version, engine_api, derived trust}, `fault` {class, mode, causes}, `detection_type` (event \| absence), `active_since` (E3), `group` (≤ 3), `route` (routed \| held), `held_by_upgrade_until` (E3 §7, optional), `lane` {value, reason}, `evidence` (≤ 10), `advice`, `runbook` {G4 id, `verify_after_s`} \| null, `would_have` {L0, L1, L2} | Router (G3) |
| `incident_updated` | `change`: `evidence_added`, `resolving` and `refiring` (E3 §4; + `eval` true/false/unknown), `routed`, `suppressed` (+ `suppressed_by`), `unsuppressed`, `reopened` (+ `reopen_count`), `escalation_simulated` (+ `simulated_failures`) | Router |
| `incident_resolved` | `how`: `cleared`, `rule_retired`, `group_retired` (E3 §4), `closed_quietly` (suppressed child); `eval` at the resolving tick | Router |
| `feedback` | `value` (agree, disagree, wrong_lane, false_alarm), `suggested_lane` (wrong_lane only), `source` (live \| replay; replay adds `replay_head`), `admin` (**session identity**, never the request body; K1 T-17), `comment` (excerpt) | API → single writer |
| `adjudication` | `verdict` (true_fault, false_alarm, watcher_caused, unknown), `true_lane`, `by`, `basis` (admin_mark, weekly_readout, log_review) | Eval tooling via API |
| `anchor` | deleted seq range, count, date range, `last_deleted_hash` | Purge (C4 §6.5) |
| `store_reset` | `reason`, `moved_aside`, `previous_head` {seq, hash} \| null, `instance_id` | Start-up after corruption (C4 §6.3) |
| `pack_event` (X1 ⚑3a) | `event`: `imported` (+ `source` bundled \| admin_import, `previous`), `reverted` and `override` (+ `by`, `previous`), `rule_skipped` (+ `rule`, `reason: needs_engine_minor`); `pack` {id, version}; `by` = admin **session identity** for admin actions | Pack loader (F6) → single writer |

- **Typed fields (K1 T-04).** Tags: `enum`, `number`, `id`, `timestamp`, `hash`, `redacted_excerpt`, `pack_text` (⚑5). `id` values use D3's label-safe pattern, so injected text can't ride in a group value or admin field (fixtures `templated-group-value`, `feedback-admin-free-text`). **Admin comments never leave the site:** H5 exports counts only.
- **Numbers are integers.** Non-integers travel as decimal strings, so the hash doesn't depend on a language's float printing (`float-value`).
- **Evidence** points at observations by D3 `id`. A value or excerpt is copied only as needed: ≤ 10 items, ≤ 500 chars each, with the K2 `redaction_version`. **Selection and order are fixed (S0 review R6),** so two correct engines write the same bytes: for each signal the rule reads, the newest observation that contributed to the tick's evaluation; ordered by (`t`, `observation_id`), and on a tie (two rule signals reading one observation) in the order the rule's `when` first uses those signals; over 10, the oldest are dropped. "Signal" here means a **rule signal** (one of the rule's named `signals`, ≤ 8), not a D1 signal ID (S4b-3, decided 2026-10-07). `evidence_added` is written only when a rule signal contributes for the first time in this incident, so an incident gets at most one per rule signal (≤ 8, E2), never one per newer observation (S0 re-check N4: keeps C4's ~200 records/day sizing).
- **Gate fields (A4):** `lane`, `detection_type` (copied from the rule's `detection`, default `event`; X1 ⚑4a), `active_since`, `rule.id`/`revision`, `instance_id`, `install_shape`, `subject`, `suppressed_by` (suppressed children don't count as separate alarms, E3 §6.1), feedback `source`, adjudication. **Changed from A4's note:** no `injection_id`. The watcher can't know about injections on partner sites, so I3 joins its labels to records by instance, cause and time window.
- **Size.** The schema's ASCII worst case is **~9.3 KB**, not C4's 8 KB (5 KB of it is excerpts). Typical records are ~2 KB. **G3 enforces a 16 KB hard cap per record** (non-ASCII excerpts): over it, drop excerpts from the last evidence item backwards and count them. → C4 Q1: if every record were worst case, 90 days at 200/day is ~170 MB (was 145 MB), and the reserve holds ~7,000 records (was ~8,000).
- **Retention** is C4's, never without an anchor. Because `at` isn't monotonic across record types (A3-5), a purge deletes **the longest seq prefix whose largest `at` is before the cutoff** (S0 review R13), and the anchor's `deleted_from`/`deleted_to` are the min and max `at` of what it deleted. The record carries no per-record expiry.

## 4. Hash chain

- **Scope: one chain per store** (install), covering every record type, feedback and adjudication included (⚑4).
- `hash = sha256(canonical JSON of the record without hash)`. Canonical = sorted keys, no whitespace, UTF-8, integers only (equal to RFC 8785 for these records, **inferred**: keys are ASCII). `prev` = the previous record's hash; the first record ever uses 64 zeros.
- **Purge:** append an anchor naming the last deleted record's hash, then delete. Verification starts at the oldest remaining record, which must be genesis, a reset, or covered by an anchor.
- **Reset:** a fresh store starts with `store_reset`, whose `prev` and `previous_head` name the old head (or zeros if unknown).
- **Errors** (`decision_chain.verify`): `E-HASH` edited, `E-SEQ` gap or reorder, `E-LINK` broken link (including an edited-and-rehashed record), `E-UNANCHORED` history missing with no anchor. Each has a test.
- The logged chain head (C4) and H5's head and anchor counts are the outside witnesses. The chain alone can't stop a fully compromised writer.

## 5. Alternatives rejected (one line each)

- **Multi-member incidents with a resolved lane:** E3 keeps one incident per rule × group and handles causes by suppression; a second grouping model would disagree with the engine.
- **Mutable incident row plus a separate audit log:** two sources of truth, and the chain would cover only half.
- **Advice by reference only (pack hash):** the store keeps two packs (C4 S5), so old records would lose their "why" (E7).
- **Floats in records:** the hash would depend on float printing, so G3 and a verifier in another language would disagree.

## 6. Assumptions and open questions

| Item | Owner |
|---|---|
| Runbook descriptors carry `verify_after_s` (30 s–24 h) | G4 |
| Feedback buttons and the comment box match `feedback` (incl. `disagree` and `suggested_lane`) | H2 |
| 16 KB record cap and excerpt truncation in the writer; both backends | G3 |
| Re-run K1 T-04 and T-17 against this schema | K1 delta (W4) |

## 7. K1 §6 row G2, verified by tests

*Every stored field typed:* `test_every_leaf_field_carries_an_allowed_type_tag`, `test_free_text_is_only_ever_excerpt_or_pack_text`, and the invalid fixtures. *Append-only, hash-chained:* `test_editing_a_past_record_breaks_the_chain`, `…rehashing_one_record_still_breaks_the_next_link`, `…deleting_a_middle_record…`, `…reordering…`, `…purge_without_an_anchor…`.

## 8. ⚑ ENG decisions (decided by Craig, 2026-10-07: 1c · 2a · 3a · 4a · 5b)

| # | Question | Options | Decision | Consequence of the alternatives |
|---|---|---|---|---|
| 1 | Multi-lane incident (G1) | (a) highest lane wins; (b) split per lane; (c) the root cause's lane | **(c)**, realised through E3 suppression (R2): only the cause routes, and the symptom keeps its own lane but doesn't route. E3 makes "several unrelated roots in one incident" impossible | (a): a lane-2 key fault with a lane-3 symptom would go to Support. (b): one outage becomes several routed incidents |
| 2 | Lane-1 incident on an untrusted runtime value (G1) | (a) keep lane 1, cap L2 at "propose"; (b) demote to lane 3 | **(a)** | (b): injection attempts would inflate the lane-3 case rate |
| 3 | Phase 0 escalation simulation (G1) | (a) 2 verify windows still open → `escalation_simulated`, lane unchanged; (b) don't simulate | **(a)** | (b): no Phase 0 evidence on how often L2 would have escalated |
| 4 | Hash-chain scope (G2) | (a) per store, all record types; (b) per incident; (c) separate chains for decisions and feedback | **(a)** | (b): deleting a whole incident leaves no trace. (c): two heads to witness, same protection |
| 5 | Free-text policy (G2) | (a) K1 strict; (b) K1 types + signed pack advice + admin comment as a redacted excerpt ≤ 500 chars, never exported; (c) as (b), comments exported in H5 | **(b)** | (a): old records lose their fix text after a pack rotates. (c): free text leaves the site |
