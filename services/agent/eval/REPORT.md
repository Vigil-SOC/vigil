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
