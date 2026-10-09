# Medic engine API 1.0

**Status:** E2 contract, Accepted (Craig, 2026-10-07). Structure: `rule.schema.json`. Loader checks: `rule_check.py` (the executable form of §6). Examples: `fixtures/rules/valid/` (11) and `fixtures/rules/invalid/` (17). Observations come from `observation.schema.json` (D3). Evaluation details (missing data, counter resets, window edges, grouping, flapping, suppression) belong to **E3**. This file fixes *what* a rule may say; E3 fixes exactly *how* it is evaluated.

## 1. Versioning

- The engine API is **`MAJOR.MINOR`**. This is version **1.0**.
- **A MINOR bump is additive only.** It may add a function, an operator, an optional field or a signal kind, or **loosen** a limit. Every rule valid under 1.N stays valid, with the same meaning, under 1.N+1.
- **A MAJOR bump is anything else:**
  - removing or renaming a function or field;
  - changing what an existing function returns;
  - tightening a limit;
  - changing a default (for example, `trusted_only`);
  - changing the observation fields that rules read.
- **A rule declares the minimum API it needs** (`engine_api: "1.N"`, default `1.0`). **A pack declares** `engine_api` = the highest of its rules, plus `fingerprint: fp1` (→ F1 manifest).
- **An engine at 1.M:**
  - refuses a pack whose MAJOR ≠ 1, and keeps the last-known-good pack (F6);
  - loads a 1.N pack with N ≤ M normally;
  - when N > M, loads every rule it can and **skips** each rule that needs a newer minor, reporting it as *not evaluated: needs engine 1.N*. A skipped rule is never reported as healthy.
- A new fingerprint algorithm (`fp2`) is a MINOR bump only if the engine keeps computing `fp1` for packs that declare it.

## 2. Rule shape

| Field | Required | Meaning |
|---|---|---|
| `apiVersion: medic.rules/v1`, `kind: Rule` | ✔ | |
| `id` | ✔ | `ingest.*`, `llm.*` or `pipeline.*` (the three v1 fault classes). Stable for the rule's life |
| `title`, `revision` | ✔ | `revision` is bumped on any change in behaviour |
| `fault` | ✔ | `class`, B5 `mode` and `causes`. Used by G1/G2 and the evidence report |
| `lane` | ✔ | 1, 2 or 3. G1's router consumes it. Lane 1 is gated (§5) |
| `engine_api` | | §1 |
| `input_trust` | | Only `untrusted` is allowed. Authors can raise trust, never lower it (§5) |
| `shapes` | | Install shapes the rule applies to (default: all). On other shapes it is *not evaluated* |
| `signals` | ✔ | 1–8 named inputs (§3) |
| `group_by` | | ≤ 3 names, each produced by a signal's `by` or `keys`. One incident per group |
| `when` | ✔ | A condition tree (§4) |
| `for`, `keep_firing_for` | | Prometheus semantics: the condition must hold this long before firing, and firing holds this long after it stops. **Defaults: E3 §4** (`for` 2 m, `keep_firing_for` 5 m), not Prometheus's 0 (X1 #12) |
| `detection` | | `event` (default) or `absence`: does the rule fire on something happening, or on something expected not happening? Copied to the decision record's `detection_type` for A4's time-to-detect bar (X1 ⚑4a). Rules built on `absent_for` or `increase == 0` declare `absence` |
| `params` | | ≤ 8 site-overridable numbers or durations, each with `default`, `min` and `max`. Conditions use them as `{param: name}` |
| `advice` | ✔ | `summary` (≤ 200 chars) and `fix` (≤ 1,000 chars), both **static text** |

## 3. Signals

A signal is one input stream, read from D3 observations whose `signal` is a **D1 signal ID** (`signal_ids.json`, 60 IDs).

- **`sample: {signal, key, by?, labels?}`** reads `values[].key` from sample observations. `labels` keeps only the values with those labels. `by` names the labels that become group keys.
- **`log: {signal, logger?, level?, template?, fingerprint?, exc_type?, message?, args?, trusted_only?, keys?}`** reads log observations of a D1 **log** signal.
  - It needs at least one of `template`, `fingerprint`, `message` or `exc_type`.
  - String fields match by exactly one of `eq`, `prefix` or `re2`.
  - `trusted_only` **defaults to true**: only lines whose `template_trust` is `catalog` count.
  - `keys` takes group keys from `logger`, `exc_type`, `fingerprint` or `args[0..3]`.
- **Log level is a filter, never a severity.** Real outages log at WARNING, and some only at INFO or DEBUG (B3).

## 4. Functions and operators

| `fn` | Input | Needs | True or false from |
|---|---|---|---|
| `latest` | sample | `op`, `value` | the newest present value |
| `age` | sample (timestamp) | `op`, `value` (seconds) | evaluation time minus the newest timestamp value |
| `increase` | sample (counter or gauge) | `window`, `op`, `value` | the rise over the window. Counters add up across `epoch` changes, so a restart isn't a drop |
| `rate` | sample (counter or gauge) | `window`, `op`, `value` | `increase` ÷ window seconds |
| `changes` | sample | `window`, `op`, `value` | how many times the value changed within the window (flapping, stalls) |
| `count` | log | `window`, `op`, `value` | matching lines in the window, de-duplicated by `line_hash` |
| `absent_for` | sample or log | `window` | true when there were successful reads for the whole window but no present value or matching line |
| `latch` | two log signals: `set`, `clear` | — | true from the latest `set` line until a later `clear` line, per group (for #1661 and #1689 enter/recover pairs) |

- **Operators:** `==` and `!=` on any value; `<`, `<=`, `>`, `>=` on numbers.
- **Values:** a number, a boolean, a label-safe string, or `{param}`.
- **Combinators:** `all`, `any` and `not`, nested at most 3 deep, with at most 16 conditions.
- **Truth has three values.** A condition is **unknown** when its signal has no `ok` observation within 2 × the sensor interval, when the sensor is `blind` or `stopped`, or when a read failed (D3 2a). Unknown propagates (Kleene logic). **A rule fires only on true**, and a rule stuck at unknown is shown as "can't evaluate", never as healthy. E3 writes the vectors.

## 5. Trust and the lane-1 gate (K1 T-05, E1 4a, D3 3a)

**Derived `input_trust`.** A rule is `untrusted` if any of these holds; otherwise it is `trusted`:

- the author set it;
- any log signal sets `trusted_only: false`;
- any log signal matches on `message` or `args`;
- any log signal takes a key from `args`.

At run time, an observation value with `trust: untrusted` (sample `text`) also taints that evaluation.

**Lane 1 needs all of the following** (checked at load time):

- the derived trust is `trusted`;
- at least 2 signals are used;
- at least one of them is a sample.

So no single log line can ever reach lane 1. G1 also enforces this at routing time (defence in depth).

## 6. Loader checks and error codes

The order is: size → YAML → schema → meaning → version. The first stage that fails stops the rule.

| Code | Refused because |
|---|---|
| `E-SIZE` | the rule file is over 16 KB |
| `E-YAML` / `E-YAML-ALIAS` / `E-YAML-DUPKEY` | not safe YAML: object-constructing tags, aliases (billion-laughs), or duplicate keys (a reviewer would see one value while the engine used another) |
| `E-SCHEMA` | `rule.schema.json` fails: unknown field (including any code or expression), missing lane, unknown function, templated advice, or `engine_api` MAJOR ≠ 1 |
| `E-SIGNAL-UNKNOWN` / `E-LOG-SIGNAL` | not a D1 signal, or the wrong kind (sample vs log) |
| `E-DETECTION` | the rule omits `detection` but has a condition, outside any `not`, that waits out a quiet gap: `absent_for`, or `increase`/`rate`/`changes`/`count` with `==` or `<=` 0 (S0 review R7). An explicit `detection` (either value) always stands; the field is declared, not derived (X1 ⚑4a). This is a lint for the common forms, not proof: `count < 1`, `not(increase > 0)`, a param defaulting to 0 and `age >` staleness aren't caught, so F7's review checklist asks |
| `E-REF` / `E-UNUSED` | a condition names an undeclared signal or param, or a signal is declared but never used |
| `E-FN-TYPE` | the function doesn't fit the signal kind (e.g. `increase` on log lines) |
| `E-RE2` | not RE2 syntax (no backreferences or lookaround), or the compiled program is larger than 1,000 (memory cap 1 MiB) |
| `E-DEPTH` / `E-LIMIT` | nesting deeper than 3, or more than 16 conditions |
| `E-WINDOW` / `E-PARAM` | a window or param max above 48 h, `for` above 24 h, or a param default outside [min, max] |
| `E-GROUP` | `group_by` names something no signal produces |
| `E-LANE1` | the lane-1 gate (§5) |

A refused rule never runs. A pack with a refused rule is refused whole (F6), and the last-known-good pack stays active.

## 7. What content may never do

Content may never:

- **run code**, or contain expressions, imports, templates or any field the schema doesn't list (K1 T-20);
- **read files or the network**: rules see only observations;
- **use a backtracking regex** (K1 T-10);
- **interpolate captured text into advice** (K1 T-05 (3));
- **lower its own trust** below the derived value;
- **reach lane 1 on one log line**;
- **reference other rules** (no composition in v1);
- **name a fix action**: Phase 1 runbooks are G4 descriptors and are not referenced from rules in v1;
- **exceed a limit in §6**.

## 8. Limits (engine API 1.0)

| Limit | Value |
|---|---|
| Rule file | 16 KB |
| Signals, conditions, nesting depth, params, group keys | 8, 16, 3, 8, 3 |
| Window and param max; `for` and `keep_firing_for` | 48 h; 24 h |
| RE2 pattern; compiled program; memory | 256 chars; 1,000; 1 MiB |
| `eq` and `prefix` strings | 500 and 200 chars |
| Advice: summary and fix | 200 and 1,000 chars |
| Groups per rule | 64 live groups; anything beyond goes into one overflow incident (E3) |
