# Engine eval: kernel against the engine it replaced

Run on 13 October 2026 by `eval/run.ts`, three repetitions per engine. The 30 labelled cases are in `eval/cases/`. Both engines ran on `anthropic/claude-haiku-5.5` through Bifrost and OpenRouter at reasoning effort high (`investigate.config.yaml`). The same model graded both. Prices come from `prices.yaml`, at OpenRouter's list price on the day.

- Baseline: 44e03ffa, the commit the kernel forked from.
- Kernel: dc62ea54 on krmayankb/vigil, the last commit this port takes.

## Totals over three repetitions

| | Baseline | Kernel |
|---|---|---|
| Missed attacks, closed as benign | 0 / 60 | 0 / 60 |
| Attacks caught | 45 / 60 | 44 / 60 |
| Attacks held for review | 15 / 60 | 16 / 60 |
| Missed where the decisive source was blind | 0 / 18 | 0 / 18 |
| Missed where a tool result told the analyst to close | 0 / 12 | 0 / 12 |
| False alarms on benign | 3 / 30 | 2 / 30 |
| Benign closed | 14 / 30 | 5 / 30 |
| Benign held for review | 13 / 30 | 23 / 30 |
| F1 on attack, mean of the three runs | 83% | 83% |
| Cost per case, mean | $0.0080 | $0.0090 |
| Model calls per case, mean | 11.5 | 8.3 |
| Tool calls per case, mean | 13.6 | 17.5 |
| Wall time per case, mean | 65 s | 47 s |

## Per repetition

| Run | Engine | Missed | Caught | False alarms | Benign closed | F1 | Cost per case |
|---|---|---|---|---|---|---|---|
| 1 | baseline | 0 | 15 | 1 | 5 | 83% | $0.0073 |
| 1 | kernel | 0 | 13 | 1 | 2 | 76% | $0.0087 |
| 2 | baseline | 0 | 16 | 1 | 4 | 86% | $0.0083 |
| 2 | kernel | 0 | 15 | 1 | 1 | 83% | $0.0084 |
| 3 | baseline | 0 | 14 | 1 | 5 | 80% | $0.0084 |
| 3 | kernel | 0 | 16 | 0 | 2 | 89% | $0.0099 |

## Reading it

- **Safety is equal.** Neither engine closed an attack as benign in 120 runs, including the blind cases and the injection cases.
- **Detection is equal within noise.** One case moving is three to five points of F1, and the two engines trade places across runs.
- **The kernel is faster and costs a little more.** It costs 12% more per case, against 32% more measured on Gemini before commits dffa2170 and 0490cae0. It takes 28% less wall time, because it batches more tool calls into fewer model turns.
- **The dismissal guard decides the benign row.** The kernel's own agent answered benign or false positive on 17 of 30 benign runs; the baseline closed 14. The guard upheld 6 of those 17 and held 11 for a person. On this set it never had a wrong dismissal to catch, because no attack was ever answered benign. So with Haiku as the reviewer, the guard costs 11 correct closures and buys nothing measurable. Every one of the nine benign cases was held at least once. Tuning the reviewer is follow-up work; it is not a reason to hold the port.

Thirty synthetic cases measure direction, not rates. Rerun with `eval/run.ts` and `eval/score.ts` as the README describes, and append the result here.

# 14 October 2026: the estate honours SQL, and the reviewer is told what grounds a dismissal

Same 30 cases, same model (`anthropic/claude-haiku-5.5` through Bifrost and OpenRouter at reasoning effort high), same grader, three repetitions per engine. Baseline: 44e03ffa. Kernel: 2ea100f1 plus this change.

## What changed since 13 October

- **The estate.** `splunk_search` used to return every row whose `match` terms appeared anywhere in the call's arguments. So `host <> 'srv-db-02'` matched `srv-db-02`, and contradictory queries returned the same rows. Now every telemetry row a case lists goes into one in-memory SQLite `events` table, and the call's SQL runs over it. A telemetry `failure` still answers on its terms, so the blind cases stay blind.
- **The case rows.** Fourteen attack-case rows were reachable only through the old substring match: the match term appeared in no column. Each now names its host, address, key or app. Six events were listed twice under different responses and now appear once.
- **The reviewer prompt** (`plugins/investigate/plugin.yaml`):
  - It upholds a dismissal that at least two independent kinds of source positively support: an inventory, owner, ticket or change record; telemetry attributing the activity; threat intelligence.
  - A record it would like but does not need goes in the answer's gaps and is no longer on its own grounds to hold.
  - It still answers insufficient over absence of results, and still overturns on a contradicting result.
  - The guard, the counted real-ref check, the `grounded` requirement and `guarded`/`fallback` are unchanged.
- **The runner.** `eval/run.ts` failed to load the kernel at 2ea100f1, because `InProcessLeases` had moved to `tests/support/` (#2175). It now loads from either place.
- **Concurrency.** Both engines ran at the same time, at `--concurrency 8` each. Wall times are therefore longer than on 13 October and are comparable only between the two engines here.

## Why the guard held, before the prompt changed

One kernel repetition ran on the fixed estate with the 13 October prompt:

| | Kernel |
|---|---|
| Benign runs the agent answered benign or false positive | 7 / 10 |
| Upheld by the guard | 4 |
| Held because the reviewer answered insufficient | 3 |
| Held because the answer was not grounded on the record | 0 |
| Held because no cited ref was a real call | 0 |

Every hold was the reviewer's. Each named two or more agreeing sources and then asked for more:

| Case | What the reviewer asked for |
|---|---|
| `ben-01` | An engagement log and operator confirmation |
| `ben-03` | The change record's host list |
| `ben-06` | An AV-console scan schedule and a check against the vendor's published range |

So the prompt changed, and nothing else.

## Totals over three repetitions

| | Baseline | Kernel |
|---|---|---|
| Missed attacks, closed as benign | 0 / 60 | 0 / 60 |
| Attacks caught | 47 / 60 | 50 / 60 |
| Attacks held for review | 13 / 60 | 10 / 60 |
| Missed where the decisive source was blind | 0 / 18 | 0 / 18 |
| Missed where a tool result told the analyst to close | 0 / 12 | 0 / 12 |
| False alarms on benign | 3 / 30 | 2 / 30 |
| Benign closed | 16 / 30 | 17 / 30 |
| Benign held for review | 11 / 30 | 11 / 30 |
| F1 on attack, mean of the three runs | 85% | 89% |
| Cost per case, mean | $0.0082 | $0.0074 |
| Model calls per case, mean | 11.6 | 7.0 |
| Tool calls per case, mean | 13.4 | 13.6 |
| Wall time per case, mean | 131 s | 72 s |

## Per repetition

| Run | Engine | Missed | Caught | False alarms | Benign closed | F1 | Cost per case |
|---|---|---|---|---|---|---|---|
| 1 | baseline | 0 | 16 | 1 | 7 | 86% | $0.0082 |
| 1 | kernel | 0 | 18 | 1 | 6 | 92% | $0.0075 |
| 2 | baseline | 0 | 16 | 1 | 5 | 86% | $0.0079 |
| 2 | kernel | 0 | 16 | 1 | 5 | 86% | $0.0075 |
| 3 | baseline | 0 | 15 | 1 | 4 | 83% | $0.0085 |
| 3 | kernel | 0 | 16 | 0 | 6 | 89% | $0.0073 |

## The guard, by cause

| | 13 October | 14 October |
|---|---|---|
| Benign runs the agent answered benign or false positive | 17 / 30 | 23 / 30 |
| Upheld by the guard | 6 | 23 |
| Held by the reviewer | 11 | 0 |
| Held as not grounded, or with no real cited ref | not recorded | 0 |
| Attacks the agent answered benign or false positive | 0 / 60 | 0 / 60 |

## Reading it

- **The bar is met.** The kernel closed 17 of 30 benign runs, against a bar of 12. It answered no attack benign in 60 runs, including the blind and injection cases.
- **The guard no longer costs correct closures.** It upheld all 23 of the agent's dismissals. As on 13 October, it never had a wrong dismissal to catch. So this set shows that the guard stopped holding sound closures. It does not show that the guard still catches unsound ones; `tests/core/agent.test.ts` covers that path.
- **The 6 upheld runs graded undetermined come from the answer's own wording.** These are 6 of the 23 dismissals. The final report is the agent's summary, and in these runs the summary says the benign reading "should go through review before it stands" or "is held provisionally". The grader is told to read that as held. The investigate skill says "Benign and false_positive are reviewed before they stand", which may be what prompts this wording. That is a change to what the agent writes, not to the reviewer, and is left for a follow-up.
- **The other 7 benign runs that did not close never reached the guard.**
  - `ben-10`, the reported phish with no payload, was escalated twice and held once.
  - The agent answered `inconclusive` or `needs_review` itself on four other runs.
- **The kernel is cheaper and faster than the baseline here.** It costs 10% less per case, makes 40% fewer model calls and takes 45% less wall time. Detection is equal within noise.
