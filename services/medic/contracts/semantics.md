# Medic evaluation semantics (engine API 1.0)

**Status:** E3 contract, Accepted (Craig, 2026-10-07: defaults 1a, static suppression 2a, automatic upgrade windows 3a, lower-bound windows 4a). **Amended 2026-10-07 by S4b** (⚑ S4b-1, 2, 4 decided (a) by Craig: `changes` lower bounds on any type, per-series staleness, only `ok` heartbeats cover; vector v28). **Amended 2026-10-08 by S10** (⚑ S4b2-3, 4, 6 decided by Craig: `closed_quietly` in §4/§6.3 and v17/v18; the upgrade hold capped at 60 min in §7, vector v29). Rule shape: `rule.schema.json` + `ENGINE_API.md` (E2). Inputs: `observation.schema.json` (D3). Executable examples: `vectors/*.yaml`. Each one is a timeline of observations plus the expected states. **If a vector and this text disagree, this text wins and the vector is fixed.**

## 1. The tick

- The engine evaluates every rule at **ticks**: every `tick` (default **15 s**), aligned to whole multiples of 15 s UTC.
- At tick T it uses every observation it has received (`observed_at` ≤ T). Windows are measured on each observation's `t` (D3 decision 1a).
- A late log line (its `t` falls in a window already evaluated) counts from the next tick on. Nothing is re-evaluated backwards.
- Replay (E6) feeds observations in `observed_at` order and ticks on the same grid, so results are deterministic.

## 2. Truth has three values

Each condition, at tick T and for one group, is **true**, **false** or **unknown**.

**Staleness.** A sample **series** (one `key` and label set) is *fresh* if its newest `ok` observation **that carries it** has t ≥ T − 2 × interval. The interval is the covering sensor's `interval_s`. An `ok` read that doesn't carry the series (a source that dropped out of a sensor still reading others) isn't a read of that series: the series goes stale, so it is unknown, never absent (S4b-2, decided 2026-10-07).

**Functions** (series = the values of one `key` and label set, after any group filter):

| `fn` | Value | Unknown when |
|---|---|---|
| `latest` | newest present value, compared. An **absent** value makes the comparison **false** (absent is a known fact; use `absent_for` to alert on it) | the signal isn't fresh, or the newest read was `error` |
| `age` | T − the newest present timestamp, in seconds | as `latest` |
| `increase` | Take the **baseline** b (the newest present sample with t ≤ T − W), then every present sample in (T − W, T]. Sum the deltas between consecutive samples. For a counter, a pair in the **same epoch** adds max(0, Δ); a pair across an **epoch change** adds the new value (the counter restarted from 0). For a gauge, it's last − b | the signal isn't fresh; or the window is **not fully covered** and the lower bound doesn't decide the comparison (below). For a gauge, any incomplete coverage gives unknown |
| `rate` | `increase` ÷ W seconds | as `increase` |
| `changes` | number of consecutive pairs (baseline included) whose values differ. For counters, pairs across an epoch change are not counted, so a restart is neither a change nor a reset of "flat" | as `increase` |
| `count` | matching log lines with t in (T − W, T], de-duplicated by `line_hash` | the window is not fully covered and the lower bound doesn't decide the comparison |
| `absent_for` | true if the **signal** was **covered** for the whole window (consecutive `ok` reads of the signal, or heartbeats, never more than 2 × interval apart, starting by T − W + 2 × interval; any read of the signal counts, so a source that dropped out is absent, unlike staleness) and had no present value or matching line in it. False if anything was present | the signal was not covered for the whole window |
| `latch` | true if the newest `set` line is newer than the newest `clear` line, per group. Equal times: clear wins. Latch state is **persisted** (C4), so a watcher restart keeps it, and it outlives the 7-day raw history | never. A watcher that starts after the `set` line can't know about it until the next `set` line (a reminder) arrives |

**Coverage and lower bounds** (`increase` and `rate` on counters, `changes` on any value type, `count`; a count of changes can only grow as more reads are seen, S4b-1, decided 2026-10-07). A window is **fully covered** if both hold:

- there is a baseline no older than T − W − 2 × interval (for `count`: the log sensor's heartbeats reach back that far);
- consecutive `ok` reads (or heartbeats) inside the window are never more than 2 × interval apart. Only a heartbeat in state `ok` covers; `degraded`, `blind` and `stopped` mean reads are failing (S4b-4, decided 2026-10-07).

If the window is not fully covered, the value computed from what was seen is a **lower bound** x. It starts from the first sample in the window when there is no baseline. The comparison is **true** if it holds for every value ≥ x, **false** if it fails for every value ≥ x, and **unknown** otherwise. Examples:

- 4 changes seen → `>= 4` is true;
- 5 arrivals seen → `== 0` is false;
- 0 arrivals seen → `== 0` is unknown.

So "no arrivals for 6 h" can't fire until the watcher has actually watched for 6 h.

**Combinators use Kleene logic:**

- `all`: false if any child is false; otherwise unknown if any child is unknown; otherwise true.
- `any`: true if any child is true; otherwise unknown if any child is unknown; otherwise false.
- `not` swaps true and false, and leaves unknown as unknown.

**Rules fire only on true.** A rule whose evaluation stays unknown is shown as *can't evaluate* (H2), never as healthy. **Built-in rule `watcher.sensor-blind`** (K1 T-09):

- one group per sensor;
- true while the sensor's newest heartbeat is `blind` or `stopped`, false while it is `ok` or `degraded`;
- `for: 2m`, `keep_firing_for: 0s`, lane 3;
- it is never a suppression child.

## 3. Groups

- **Group key.** The values of `group_by`, taken from each signal's `by` labels or log `keys`. A rule without `group_by` has one group, `{}`.
- **Label-safe values (X1 ⚑1a, 2026-10-07; who applies it: S0 re-check N1, decided 2026-10-07).** Sample labels arrive label-safe: the **sensor** applies the rule when it builds the observation (D5; the observation schema enforces the pattern), and the engine takes them as they are. The **engine** applies it only to group keys it takes from log fields (`args`, `exc_type`, `logger`), which are free text. Either way it runs exactly once, every group value goes through D3's label rule (`fingerprint_ref.label_safe`): a value that doesn't fit the label pattern, or that looks like an engine marker (`h_…`, `__…`), becomes `h_` + 16 hex of its sha256. Log `args` and `exc_type` are free text, so `"Azure Sentinel"` groups as `h_…`. The raw value is never a group key; it travels only as a K2-redacted evidence excerpt (G2).
- **Filtering.** A signal that produces the group's labels contributes only its matching values. A signal that doesn't produce them is **shared** by every group, and must have exactly one series; otherwise the condition is unknown (reason `ambiguous_series`).
- **Group lifetime (X1 ⚑2a; refined after the S0 review, R1–R3, decided 2026-10-07).** Groups are discovered from observations. An **observation of a group** is any observation from one of the rule's signals that carries the group's label values (for a log signal: a matching line whose `keys` give that group). A group **retires** at the first tick T with T − (the `t` of its last observation) ≥ 24 h, with no `absent_for`-style slack, **and every sensor covering the rule's signals** was covered (§2: `ok` reads or heartbeats never more than 2 × interval apart) for that whole 24 h; a blind period restarts the 24 h. **Three groups never retire:** the single `{}` group of a rule without `group_by`; the overflow group; and a group whose latch is **set** (a latched fault goes quiet by design, and §2 keeps the latch beyond the raw history). A cleared latch group retires like any other. If a retiring group has an open incident (firing or resolving), the incident resolves at T with reason **`group_retired`**, without a false tick or `keep_firing_for`: the thing it was about no longer exists, e.g. a deleted integration. Its state is dropped; a group that reappears later starts fresh with a new incident. Vectors v23–v26.
- **Cap.** At most **64 live groups per rule**, in order of first appearance (ties broken by sorting the label values). Further groups are evaluated together as one overflow group, which is true if any of them is true. Its key sets every `group_by` label to `__overflow__`, and its incident lists no per-group detail.

## 4. The state machine (per rule × group)

```
            true                 true for ≥ for                  false
inactive ───────▶ pending ─────────────────────────▶ firing ─────────────▶ resolving
   ▲   false        │  unknown: stay (timer keeps     │  unknown: stay        │ true: back to firing
   └────────────────┘   running)                      │  firing (never        │ false at a tick with
   ▲                                                  │  resolve blind)       │ T − since ≥ keep_firing_for
   └──────────────────────────────────────────────────┴───────────────────────┘ → incident resolved
```

- **pending.** Records `active_since` = the first true tick. An unknown tick **neither resets nor fires**. A false tick returns to inactive. A true tick with T − `active_since` ≥ `for` → **firing**: the incident opens with `opened_at` = T. With `for: 0s`, firing happens on the first true tick.
- **firing → resolving** on the first **false** tick, with `resolving_since` = T. An unknown tick keeps the incident firing.
  - A true tick in resolving returns to firing; it's the same incident.
  - Resolution needs a **false** tick (not unknown) with T − `resolving_since` ≥ `keep_firing_for`. Unknown ticks hold the incident open. **A blind watcher never auto-resolves.** With `keep_firing_for: 0s`, the first false tick resolves the incident at once, and the state is back to inactive.
- **Resolve reasons:** `cleared` (the normal case), `closed_quietly` (a suppressed child that resolves without ever routing, §6.3; amended 2026-10-08, ⚑ S4b2-3), `rule_retired` (the pack update removed the rule), `group_retired` (§3). The two retirements end the incident at once, with no `resolving` phase, and win over `closed_quietly`. A changed `revision` keeps open incidents open.
- **Defaults when a rule omits them:** `for` **2 m**, `keep_firing_for` **5 m** (⚑ 1).
- **Persistence.** All of this state is persisted with each transition (C4). A restart resumes it; it never resets it.

## 5. Flapping (incident damping)

If one rule × group **opens 3 incidents within 60 min**, the third does not open a new incident. Instead it **reopens the most recent one** and marks it `flapping: true`. A flapping incident resolves only after the condition has been **not true for 30 min** in a row (this replaces `keep_firing_for`), and it ends with a false tick. `reopen_count` records each re-entry (⚑ 1).

This is separate from **signal flapping**: a rule can *detect* a flapping signal with `changes` (vector v14). Damping stops a borderline rule from paging over and over.

## 6. Dependency suppression

**Source: static content in the pack**, `suppression.yaml` (→ F1), not inside rules (E2 decision 2a). Each entry has a `parent` selector and a `children` selector. A selector is a `{rule}`, `{mode}`, `{cause}` or `{class}`. An optional `match: [label]` requires the parent's and the child's group labels to agree. The B5 pairs, as content:

| Parent | Children |
|---|---|
| `{class: llm}` (any open class-2 incident) | `{mode: P-4}` |
| `{mode: L-5}`, `{mode: L-3}` | `{cause: P-2.e}` |
| `{mode: P-1}`, `{cause: P-1.c}` | `{cause: P-6.e}` |
| Postgres-down (PK-A, parked; the selector is a no-op until such a rule exists) | every "1/2 (DB)" cause |

**Semantics:**

1. **The child is suppressed** while any parent incident is **open** (firing or resolving; any group, unless `match` applies). It still runs through the state machine, and its decision record (G2) gets `suppressed_by`. It is **not routed** (G1) and is **not counted** as a separate alarm in I1 metrics.
2. **Routing hold.** A rule that appears as a child anywhere waits **5 min** after firing before it routes. A parent that fires within that hold suppresses it retroactively, because symptoms often cross their `for` before the root cause does.
3. **The child outlives the parent.** When the last parent resolves, a child that is **still firing with its condition true 10 min later** is unsuppressed and routes on its own (it wasn't only a symptom). A child that is resolving at that mark waits: if it refires true it routes then, and if it resolves it never routes. A child that resolves while still suppressed, within those 10 min or after them, **closes quietly**: its `incident_resolved` says `how: closed_quietly` (G2), not `cleared` (amended 2026-10-08, ⚑ S4b2-3, S4b2-5).
4. **Routing times.** An incident that is no rule's child routes when it opens. A child routes when its 5-min hold ends, if no parent incident is open. Every incident routes no earlier than the end of an upgrade window.
5. **The loader refuses cycles** (F6). With chained suppression, the child points at its nearest firing parent.

## 7. Upgrade windows

The engine opens an **upgrade window** when:

- the `version` signal changes; or
- restart markers for **2 or more Vigil services fall within 5 min** of each other (an epoch change, a container `started_at` change, or an `uptime_seconds` drop).

The window lasts **15 min after the last such restart** (⚑ 4). **A chain of windows** (each marker arriving before the window it extends has closed) **holds for at most 60 min after the chain's first marker**: a `version` value that keeps flipping can't hold alerts indefinitely. At the cap the window is over for every rule, **`watcher.sensor-blind` included**; later markers in the same chain extend nothing. A marker after the chain's window has closed (15 min with none) starts a new chain with its own cap (amended 2026-10-08, ⚑ S4b2-6, vector v29). During it, **pending can't promote to firing**, firing incidents carry on, and every evaluation is recorded as normal. When the window closes, any rule that is still true fires at once, because its `active_since` was kept. **An upgrade delays an alert; it never hides one** that persists. Admin-declared maintenance windows use the same mechanism. Where they are configured is K3's call; they're not in Phase 0.

## 8. What the vectors pin

**Two formats, two folders (X1 #7).** E3's engine vectors are `vectors/v*.yaml` (offsets such as `+5m`, schema `vector.schema.json`). G1's routing vectors are `vectors/lanes/lanes-*.yaml` (integer seconds, read by `lane_ref.py`). Issues name the glob they must pass.

There is at least one vector per section above. `engine_restarts` marks instants where the engine process restarts; state must survive them (C4). Each vector lists rules (inline, or a reference to `fixtures/rules/valid/`), sensors with up/down periods, compact observations (`vector_expand.py` expands them into D3 observations that validate against `observation.schema.json`), and **checkpoints**: `{at, rule, group, eval, state}` plus a final incident list. `tests/test_vectors_wellformed.py` checks the structure, that the rules pass the loader, that the expanded observations are valid D3, and that **every expected timeline obeys §4–§7** (legal transitions, `for` and `keep_firing_for` respected). The engine (E4) must make every checkpoint pass.
