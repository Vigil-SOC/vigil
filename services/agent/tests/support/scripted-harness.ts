import { budgetOf, FRESH, unmeteredQuota, type Seed } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import type { Memory, State } from "../../core/seams.js";
import type { RunSpec } from "../../core/spec.js";
import type { HarnessFactory } from "../../harness.js";
import { scriptedProvider, type ScriptedTurn } from "./scripted-provider.js";

// The model is the only part of a run that reaches outside the process, so it is the
// only part stood in for: the budget, dispatch and ledger a test drives are all real.
export function scriptedHarness(script: readonly ScriptedTurn[]): HarnessFactory {
  return <K extends Record<string, unknown>>(
    spec: RunSpec,
    state: State<K>,
    memory: Memory = nullMemory,
    // Honoured rather than ignored: seeding from the ledger is what stops a resumed
    // run spending its cap again, and a double that dropped it hides that.
    seed: Seed = FRESH,
  ): Harness<K> => ({
    provider: scriptedProvider(script),
    // Empty on purpose: a test of the wiring grants nothing, so a run that tried to
    // call a tool fails loudly rather than reaching a stand-in nobody declared.
    registry: registryOf([], {}),
    dispatch: localDispatch,
    budget: budgetOf(spec.budgets, unmeteredQuota, Date.now, seed),
    memory,
    state,
  });
}
