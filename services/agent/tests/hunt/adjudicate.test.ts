import { Ajv } from "ajv";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import type { AgentEvent } from "../../contracts/events.js";
import { buildSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { BACKWARD_NULL_HYPOTHESIS, BASE_RATE_PROVENANCE, startHunt } from "../../workflows/hunt/controller.js";
import { InProcessDirectiveQueue } from "../../workflows/hunt/directives.js";
import { newId } from "../../workflows/hunt/ids.js";
import type { HuntKinds } from "../../workflows/hunt/journal.js";
import { huntProjection } from "../../workflows/hunt/projection.js";
import { replay } from "../../workflows/hunt/replay.js";
import { ScriptedDisconfirmationCritic, ScriptedWorkerDispatcher } from "../../workflows/hunt/scripted.js";
import type { Decision } from "../../workflows/hunt/types.js";
import { CONCLUDE, controllerFor, huntSpecFor, INVESTIGATE, provable, ruled, SEED_IP, validateOn } from "../support/hunt.js";

// The kind is the hunt loop with a different caller: the lead is handed a finding
// intake admitted and the workflow intake chose, both in the hypothesis text, and
// proposes a workflow rather than starting one. Nothing below is adjudicate-specific
// code -- that is the point. The loop, the projection and the replay are the hunt's.
const INTAKE_LINE =
  "intake admitted finding f-4821 (10.128.0.5 beaconing to 198.51.100.7 every 300s) and chose incident-response;" +
  " the host is under command and control";

const ADJUDICATED: Decision & { proposed_workflow: string } = {
  ...CONCLUDE,
  rationale:
    "The interval survived the benign account across two domains; this warrants incident-response." +
    " I agree with intake: containment comes before any further hunting.",
  proposed_workflow: "incident-response",
};

// The lead schema as the loader hands it to the model, so what the scripted lead
// emits below is held to the same shape a real one would be.
const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const leadSchema = () => {
  const entry = archFor("adjudicate");
  const spec = buildSpec(
    { arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") },
    entry.actions,
  );
  return new Ajv({ allErrors: true, strict: false }).compile(spec.roles.lead!.output_schema!);
};

async function adjudication() {
  const state = new InProcessState<HuntKinds>();
  const queue = new InProcessDirectiveQueue();
  const runId = newId("run");
  const spec = { ...huntSpecFor({ hypotheses: [INTAKE_LINE], hypothesisLoop: true }), arch: "adjudicate" };
  const ledger = await startHunt(state, queue, runId, spec, "test", "adjudicate");
  return { state, runId, ledger };
}

describe("what the adjudicate lead may emit", () => {
  // Escalation is refused at the schema, not by the loop: HUNT_LOOP still handles
  // HANDOFF_IR for hunt and root_cause, so this arch's enum is the one guard.
  it("carries proposed_workflow on CONCLUDE and refuses HANDOFF_IR", () => {
    const valid = leadSchema();
    expect(valid({ ...ADJUDICATED, evidence_citations: [] })).toBe(true);
    expect(valid({ action: "HANDOFF_IR", rationale: "escalate", evidence_citations: [], target_hypothesis_id: "h-1" })).toBe(false);
    // Optional: a lead that names the workflow only in prose has still adjudicated.
    expect(valid({ action: "CONCLUDE", rationale: "none; intake was wrong", evidence_citations: [] })).toBe(true);
  });
});

describe("a scripted adjudication runs the hunt loop end to end", () => {
  it("is stamped with its own kind and seeded with the forward null", async () => {
    const { state, runId, ledger } = await adjudication();
    await ledger.flush();

    const [opened] = await state.read(runId);
    expect(opened?.run_kind).toBe("adjudicate");
    // The forward base rate, as any hunt gets: the finding is what is on trial,
    // so "this has a legitimate explanation" is the account the intent must beat.
    const [seeded] = [...ledger.projection.hypotheses.values()].filter((h) => h.provenance === BASE_RATE_PROVENANCE);
    expect(seeded?.statement).toContain(`"${INTAKE_LINE}" has a legitimate explanation`);
    expect(seeded?.statement).not.toBe(BACKWARD_NULL_HYPOTHESIS);
  });

  it("reaches CONCLUDE carrying proposed_workflow, and projects and replays unchanged", async () => {
    const { state, runId, ledger } = await adjudication();
    const [intent] = [...ledger.projection.hypotheses.values()].filter((h) => h.provenance !== BASE_RATE_PROVENANCE);
    const intentId = intent!.hypothesis_id;

    // Turn 1: a worker is asked and answers with something the lead must rule on.
    const asked = await controllerFor(ledger, [{ ...INVESTIGATE, target_hypothesis_id: intentId }], {
      dispatcher: new ScriptedWorkerDispatcher([
        {
          source_system: "net_flow",
          summary: `10.128.0.5 -> ${SEED_IP.value}: 412 connections, 300s +/- 4s`,
          payload: { conn_count: 412, interval_s: 300 },
          salience: "anomalous",
          why_notable: "regular interval with low variance",
          provenance: "worker",
          attacker_influenceable: false,
          instruction_like: false,
        },
      ]),
    }).advanceIteration();
    expect(asked.note).toBe("");
    expect(ledger.projection.evidence.size).toBe(1);

    // Turn 2: the intent is put up for a verdict and survives the critic.
    const citations = provable(ledger, intentId);
    await controllerFor(ledger, [ruled(ledger, validateOn(intentId, citations))], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();
    expect(ledger.projection.hypotheses.get(intentId)?.status).toBe("proven");

    // Turn 3: the adjudication itself.
    const result = await controllerFor(ledger, [ruled(ledger, ADJUDICATED)]).advanceIteration();
    expect(result.hunt_status).toBe("terminal");
    expect(result.hunt_outcome).toBe("completed");

    // The proposal rides on the journaled decision, untouched by the controller.
    const concluded = ledger.projection.decisions.at(-1)!.decision as Decision & { proposed_workflow?: string };
    expect(concluded.action).toBe("CONCLUDE");
    expect(concluded.proposed_workflow).toBe("incident-response");

    await ledger.flush();
    const events = await state.read(runId);

    // The registry's projection is the hunt's, handed the ledger the way serve.ts does.
    const erased = events as unknown as readonly AgentEvent<Record<never, never>>[];
    const view = archFor("adjudicate").projection?.(runId, erased) as ReturnType<typeof huntProjection>;
    expect(view).toEqual(huntProjection(runId, events));
    expect(view.status).toBe("terminal");
    expect(view.hypotheses.find((h) => h.hypothesis_id === intentId)?.status).toBe("proven");

    // Every decision rebuilds against the digest it was shown.
    const replayed = replay(ledger.log);
    expect(replayed.decisions.map((d) => d.action)).toEqual(["INVESTIGATE", "VALIDATE", "CONCLUDE"]);
    expect(replayed.reproduced).toBe(3);
    expect(replayed.decisions.every((d) => d.mismatch === null)).toBe(true);
  });
});
