import { join } from "node:path";
import { archFor } from "../../arch/registry.js";
import { budgetOf, FRESH, unmeteredQuota } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { registryOf } from "../../core/registry.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import type { Memory, State } from "../../core/seams.js";
import type { Provider } from "../../core/provider.js";
import type { HuntKinds } from "../../workflows/hunt/journal.js";

// The one hunt that reads episodic memory, shared by the test that asserts the
// read and the script that records the fixture. Two copies of it would agree
// until the day one of them was edited -- the same reason recalled.ts holds one
// set of rows rather than four.

const FIXTURES = join(import.meta.dirname, "..", "fixtures");

// The operator's own hypothesis and the subject they keyed to it: the declared
// path, which is what a run started from the console takes.
export const ASKED = "192.0.2.10 is beaconing to attacker-controlled infrastructure";
export const SUBJECT = "ip:192.0.2.10";

export interface Asked {
  operator?: readonly string[];
  hypotheses?: readonly string[];
  maxCalls?: number;
}

// Where a start job puts them: the hunt spec reads its own vocabulary off the
// sections, and subjects arrive as keys the spec parses. Built through the loader
// rather than as an object, because runHunt re-derives a HuntSpec from sections
// and an object-built one arrives with none.
export const recallHuntSpec = (asked: Asked = {}): RunSpec => {
  const entry = archFor("hunt");
  const spec = buildSpec(
    { arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") },
    entry.actions,
  );
  return {
    ...spec,
    budgets: asked.maxCalls === undefined ? spec.budgets : { ...spec.budgets, max_calls: asked.maxCalls },
    sections: {
      ...spec.sections,
      hypotheses: [...(asked.hypotheses ?? [])],
      operator_hypotheses: [...(asked.operator ?? [ASKED])],
      operator_hypothesis_subjects: { [ASKED]: [SUBJECT] },
    },
  };
};

// No tools and a local dispatch: what this hunt exercises is the read at its
// start, and a registry would only give the lead somewhere else to spend.
export const recallHarness = (spec: RunSpec, provider: Provider, memory: Memory, state: State<HuntKinds>): Harness<HuntKinds> => ({
  provider,
  registry: registryOf([], {}),
  dispatch: localDispatch,
  budget: budgetOf(spec.budgets, unmeteredQuota, Date.now, FRESH),
  memory,
  state,
});
