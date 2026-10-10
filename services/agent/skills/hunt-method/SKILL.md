---
name: hunt-method
description: How to move a hunt from its hypotheses to verdicts -- what to query next, when to validate, when to stop. Load at the start of a hunt.
---

# Running the hunt

## Work the board, not a hunch

Start from the board in your brief. For each active hypothesis, say what it
predicts you would see if it were true and what the benign explanation predicts
instead. Query for what separates them.

## One step down, or one step across

- **Deeper** keeps the same entity and hypothesis and asks one layer down: the
  same host over a longer window, the same flow at a finer grain.
- **Across** moves to something a record already showed you: 10.128.0.5 was
  beaconing; now ask what 198.51.100.7 also talked to. Record it as a
  follow_up when you record the evidence that showed it, so it is on the board.
  Pivot only to an entity a record mentions.

## Rule before you ask for more

After every record_evidence, rule each new record against every active
hypothesis with link_evidence. Evidence that only ever accrues to the hypothesis
it was gathered for is confirmation drift; ruling against the benign
explanation is what stops a hunt only looking one way.

## When to validate

Validate a hypothesis when its linked evidence would survive a defender's best
benign story: corroborated by more than one telemetry domain, resting on at
least one value the sensor recorded rather than one the adversary chose. A
verdict that is refused tells you what is missing; go and get it, then validate
again. A hypothesis you never put up for a verdict closes inconclusive when the
budget runs out, which is worse than testing it.

## Dropping a branch

abandon_hypothesis needs a cited record the telemetry attested. A feed label, a
process name, a signing cert and any auto-enriched record are all
attacker-nameable and cannot clear a branch on their own.

## Escalating

handoff_ir only on a proven hypothesis. Say in the rationale what a responder
should act on first.

## Stopping

Call conclude_hunt when nothing active could still be moved by a query you can
run. If it is refused, it says what is blocking: resolve that, or close the
open questions you have answered with close_question.
