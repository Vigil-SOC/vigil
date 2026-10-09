# Log fingerprint spec · algorithm `fp1`

**Status:** D3 contract, Accepted (Craig, 2026-10-07). Executable reference: `contracts/fingerprint_ref.py`. Frozen examples: `contracts/fixtures/fingerprint/examples.json`. A production fingerprinter (D5) must reproduce every expected value there. Vigil read at `76abec1a`.

A fingerprint names **the call site and kind of failure** behind a log line, so that a rule can say "this line, again" without matching on text that changes. Two lines that differ only in IDs, numbers, hosts, timestamps or args share a fingerprint. Lines from a different service, level, logger, template or exception type do not.

## 1. Inputs per log format (D1)

| `format` | Who emits it | Fields used |
|---|---|---|
| `python_json` | backend, soc-daemon, llm-worker (default since #1593) | `level`, `logger`, `msg_template`, `message`, `exc_type`, `exception` (`core/telemetry.py:513-551`) |
| `agent_json` | agent-worker, agent-serve | `level`, `logger`, `msg` (meant to be constant), `error_type` (`services/agent/core/log.ts:19-27`) |
| `text` | foreground `start.sh`, Bifrost, backup loop | the raw line. The daemon's `%(asctime)s - %(name)s - %(levelname)s - %(message)s` and `LEVEL msg` are parsed for logger and level. Otherwise the whole line is the message, at level INFO |

**Vigil's JSON logs don't carry the args.** `msg_template` is `record.msg` and `message` is the rendered text. Args are recovered by aligning the message against a `%`-style template (§4), so they're always untrusted.

**Order of operations:** sensor reads the line → cap at 16 KB → **K2 redaction** → fingerprint → observation. The fingerprinter never sees unredacted text.

## 2. Template trust (K1 T-05)

496 of 1,041 Vigil log calls are f-strings, so `msg_template` often holds attacker-reachable values. A template is **`catalog`** (trusted) only when both hold:

1. `(logger, template)` exactly matches an entry in the **template catalog**. The catalog is content shipped in the pack: the `%`-style templates the pack's rules use, generated from Vigil source at a release (F1/F5).
2. If the template has placeholders (`%s`, `%d`, `%(name)s`, …), `message != template`. In other words, it really was rendered from args. An f-string whose text happens to equal a catalog entry fails this test.

Everything else is **`untrusted`**. That includes `agent_json` `msg` (constant by convention, not by enforcement) and all `text` lines.

## 3. Basis text

- `catalog` → basis is the template, with whitespace collapsed (`fingerprint_basis: template`).
- `untrusted` → basis is `normalize(template or message)` (`fingerprint_basis: normalized`).

`normalize` takes the first line only, then applies these RE2 substitutions **in order** (earlier rules consume text later ones would split), collapses whitespace and caps the result at 160 chars:

| # | Replaces | With | Example in → out |
|---|---|---|---|
| 1 | ANSI colour codes | (nothing) | |
| 2 | URLs `scheme://…` | `<url>` | `https://splunk.a.example:8089/x` → `<url>` |
| 3 | Emails | `<email>` | `bob@example.com` → `<email>` |
| 4 | `'…'` and `"…"` quoted strings | `<str>` | `server at "10.0.0.5"` → `server at <str>` |
| 5 | UUIDs | `<uuid>` | |
| 6 | ISO-8601 timestamps | `<ts>` | `2026-10-07T10:00:01.5Z` → `<ts>` |
| 7 | Paths with ≥ 2 segments | `<path>` | `GET /api/v1/findings/abc123` → `GET <path>` |
| 8 | IPv4, with an optional port | `<ip>` | |
| 9 | Hex runs of ≥ 8 chars | `<hex>` | |
| 10 | Numbers | `<num>` | `after 12 attempts` → `after <num> attempts` |

## 4. Args (answers E1's question)

For a template with placeholders, the placeholders are turned into `(.*?)` and the message is matched against the literal parts (RE2). This yields at most 8 args of at most 200 chars each, stored in `log.args` and **always untrusted**. Example: `%s configuration incomplete (missing: %s); …` + the rendered line → `["Azure Sentinel", "client_secret"]`. Rules may group on args; the group value is then passed through D3's label rule (`label_safe`: `h_` + 16 hex when the arg isn't label-safe, X1 ⚑1a), and the raw arg appears only as a redacted excerpt. E2 decides whether grouping on untrusted args taints a rule's `input_trust` (E1 decision 4a says the engine derives this).

## 5. Stack frames

Only when there's an `exception` (Python JSON):

- Use the last traceback block. With chained exceptions, that's the one raised last.
- Keep the **first-party** frames (`core/…`, `services/…`; never `site-packages`) as `path:function`, **without line numbers**. Line numbers move with every release.
- Keep the innermost 5.
- `frames_sig` = 16 hex of the sha256 of the frames joined with `|`.
- The raw traceback is **not stored**. The observation keeps `exc_type` and `frames` only (K1 T-01, minimise).

## 6. The hash

```
parts = ["fp1", basis, service, logger or "", LEVEL, basis_text, exc_type or "", frames_sig]
fingerprint = "fp1_" + sha256("\x1f".join(parts))[:16 hex]
```

- **`basis` is hashed.** An untrusted line can therefore never share a trusted line's fingerprint, even with identical text. I found this while writing the tests: without it, a forged line equal to a catalog template collided with the real one.
- **`service` is hashed.** The same library code logging from backend and daemon gets two fingerprints, because they are different faults. So `service` comes from a **closed, shape-independent list** (`observation.schema.json` `$defs/service` = `fingerprint_ref.SERVICES`, X1 #6): a sensor reports Helm's `<fullname>-soc-daemon` as `soc-daemon`, and `fingerprint()` refuses any other name.
- **The algorithm version is the first part.** Any change to these rules is `fp2`, never a silent change to `fp1`. A pack declares which algorithm its rules use.

## 7. Known limits

- **f-string sites are best-effort.** Normalising raw exception text keeps cause words (`Connection refused` vs `password authentication failed`). That is useful, but a library's wording change makes a new fingerprint. Rules for those sites should match the normalized text with a prefix rather than pin one fingerprint. Converting the starter-rule sites to `%`-style (B3 option B) removes the problem.
- **Messages that are too stable** (B3 #8). Federation `last_error` is one constant string per source for every cause. A fingerprint can't split what Vigil merged. That needs a Vigil fix (→ D2).
- **One fault, several lines** (B3 #10: MCP logs 3 ERRORs per failure). Fingerprints stay per line. Correlation uses `trace_id` and time, which is E3's grouping.
- **Plain-text tracebacks** (foreground `start.sh`) arrive as separate lines and get no frames.
- **Client-written lines** (the `frontend` logger, B3-X1) can reach `logger` and `level` with arbitrary text. They are always untrusted, and the catalog must never include the `frontend` logger.
