# Engine eval

Thirty labelled alerts, run as investigate cases through the kernel and through
the engine it replaced, on the same model, the same estate and the same budget.
It answers one question first: does the kernel close attacks as benign more or
less often than the old engine did? Then: at what cost.

## What a case is

`cases/*.yaml`, one list per file. Each case is an alert as an analyst would get
it, the truth (`attack` or `benign`), why, and the estate it runs against: what
each tool returns. Telemetry (`splunk_search`) is a table: every row the case lists
for it goes into one in-memory `events(time, host, user, source, event, detail)`
table, and the call's SQL runs over it, so a query's filters decide what comes back
and an invalid query fails with SQLite's error. A telemetry `failure` still answers
any call whose arguments hold all its `match` terms, so a blind source stays blind.
The other tools answer with the first response whose `match` terms all appear in
the call's arguments. Anything a case does not mention answers with nothing, which
is what an empty search looks like. Nothing reaches a real system; addresses are
documentation ranges.

| set | n | built to catch |
|---|---|---|
| `attacks.yaml` | 10 | attacks the estate answers clearly |
| `blind.yaml` | 6 | attacks whose decisive source times out, is down, or is empty |
| `injection.yaml` | 4 | attacks where a tool result tells the analyst to close the case |
| `benign.yaml` | 10 | benign activity, seven of them dressed as attacks with the proof findable |

## What is measured

Each run's final report (the summary on its terminal) is read by one grader
model, the same for both engines, as `attack`, `benign` or `undetermined`. The
old engine ends on prose and the kernel on a structured answer, so neither's own
fields can be compared with the other's; the kernel's own disposition and any
answer its guard changed are recorded beside the grade.

- **Missed attacks**: attacks closed as benign. The costly error, and the headline.
- Caught and held: an attack escalated, or held for a person. Held is not missed.
- False alarms: benign cases escalated, on lookalikes especially.
- Precision, recall and F1 with `attack` as the positive class; undetermined counts as not-attack, so holding everything scores no recall.
- Cost per case (priced from `prices.yaml`), model and tool calls, wall time.

## Running it

The latest result is in `REPORT.md`. The estate needs Node 22.13 or later, for `node:sqlite`. `investigate.config.yaml` routes the model through
OpenRouter at high effort, so `--model` names it without the provider prefix.

```sh
# once: the engine the kernel replaced, at the commit this branch forked from
eval/setup-baseline.sh                          # -> ../vigil-eval-baseline

# the gateway must serve the model: BIFROST_URL (default http://127.0.0.1:8080)
npx tsx eval/run.ts --engine ../../../vigil-eval-baseline --label baseline --model anthropic/claude-haiku-5.5 --grader-model openrouter/anthropic/claude-haiku-5.5
npx tsx eval/run.ts --engine ../..                        --label kernel   --model anthropic/claude-haiku-5.5 --grader-model openrouter/anthropic/claude-haiku-5.5
npx tsx eval/score.ts eval/results/baseline.jsonl eval/results/kernel.jsonl --md eval/results/report.md
```

- `--only id,id` reruns a few cases; the scorer keeps the last line per case.
- `--grader-model` grades with a different model than the one under test.
- `--concurrency n` (default 2) runs cases in parallel.
- `--dry` runs every case on a model that reads the finding once and concludes
  nothing. It checks the wiring for an engine without a gateway and measures nothing.

Each engine is loaded from its own checkout and started through that checkout's
own `worker.advance()`. Three seams are replaced and nothing else: tools answer
from the case, memory recalls nothing, and prices come from `prices.yaml`. Check
those prices against the provider's current list before reading the cost row.

## Reading it with care

Thirty synthetic cases measure direction, not rates: one case is three points.
Run each engine at least three times before reading a difference of one or two
cases, and read the "where they disagree" table, which lists the cases that moved.
