// Attacker-caused is not attacker-authored. In a hunt the adversary's own behaviour is
// the whole of the signal, so every record is attacker-caused; a verdict has to ask
// something narrower -- whether the values it rests on were attested by the telemetry
// or chosen by the adversary. One real run flagged all eight of its records
// attacker-influenceable and could not have proven anything at any budget.
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../plugins/kinds.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { ToolResult } from "../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import type { Message, Provider, ProviderEvent, ToolCall, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { evidenceFrom } from "../../plugins/hunt/adapters.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { fold, type HuntEvent, type HuntKinds } from "../../plugins/hunt/ledger.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { DEFAULT_VERDICTS } from "../../plugins/hunt/config.js";
import { evidenceStrength, sensorAttested, unmetPredicates } from "../../plugins/hunt/strength.js";
import type { EvidenceRecord } from "../../plugins/hunt/types.js";
import { evidenceOn, newLedger } from "../support/hunt.js";

const SENSOR = { field: "conn_count", authored: "sensor" as const };
const AUTHORED = { field: "filename", authored: "adversary" as const };
const FEED = { field: "threat_label", authored: "third_party" as const };

// A whole hunt on the kernel, as tests/hunt/kernel-hunt.test.ts drives one: a model
// that reads ids off what it was shown, and an estate that answers with rows.
const RUN = "7d3c2d3e-0000-4000-8000-00000000a7e5";
const SPEC: RunSpec = (() => {
  const entry = archFor("hunt");
  const fixtures = join(import.meta.dirname, "..", "fixtures");
  const spec = buildSpec({ arch: entry.arch, playbook: join(fixtures, "hunt.playbook.yaml"), config: join(fixtures, "hunt.config.yaml") }, entry.actions, entry.owned);
  return { ...spec, sections: { ...spec.sections, hypotheses: ["a host is beaconing to C2"] } };
})();

const shown = (request: TurnRequest) => request.messages.map((message: Message) => message.content).join("\n");

function hypothesisIn(request: TurnRequest): string {
  const found = /- (h-[0-9a-f]+) \[active\]/.exec(shown(request));
  if (found === null) throw new Error("no hypothesis on the board");
  return found[1]!;
}

function evidenceIn(request: TurnRequest): string {
  const found = [...shown(request).matchAll(/ev-[0-9a-f]{12}/g)].at(-1);
  if (found === undefined) throw new Error("no evidence id in what the model was shown");
  return found[0];
}

type Step = (request: TurnRequest) => { tool: string; args: unknown } | { say: string };

function modelOf(steps: readonly Step[]): Provider {
  let at = 0;
  return {
    model: "scripted/model",
    provider_type: "scripted",
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      if (request.emit !== undefined) {
        const properties = (request.emit["properties"] ?? {}) as Record<string, unknown>;
        const answer = "benign_explanation_stands" in properties
          ? { benign_explanation: "a backup job", benign_explanation_stands: false, rationale: "too regular" }
          : { summary: "s", what_happened: "w", next_steps: [] };
        yield { type: "text_delta", text: JSON.stringify(answer) };
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      const step = request.tools.length === 0 ? undefined : steps[at];
      if (step !== undefined) at += 1;
      const next = step?.(request);
      if (next !== undefined && "say" in next) yield { type: "text_delta", text: next.say };
      yield { type: "usage", tokens: ZERO_TOKENS };
      if (next !== undefined && "tool" in next) {
        const call: ToolCall = { id: `c${at}`, tool: next.tool, args: JSON.stringify(next.args) };
        yield { type: "tool_call", call };
      }
    },
  };
}

function harnessOf(spec: RunSpec, provider: Provider, state: InProcessState<HuntKinds>): Harness<HuntKinds> {
  return {
    provider,
    registry: registryOf([], {}),
    dispatch: { invoke: async (tool, args): Promise<ToolResult> => (tool.local ? tool.invoke(args) : { ok: true, rows: [], rowCount: 0, capped: false, sourceSystem: tool.id }) },
    budget: budgetOf({ ...spec.budgets, max_calls: 40 }, unmeteredQuota),
    memory: nullMemory,
    state,
  };
}

const record = (over: Partial<EvidenceRecord> = {}): EvidenceRecord =>
  ({
    evidence_id: "ev-1",
    dispatch_id: null,
    iteration: 1,
    source_system: "net_flow",
    summary: "3,885 connections at a 30s interval",
    payload: {},
    salience: "anomalous",
    why_notable: "low-jitter beaconing",
    provenance: "worker",
    attacker_influenceable: true,
    instruction_like: false,
    entities: [],
    captured_at: new Date().toISOString(),
    ...over,
  }) as EvidenceRecord;

describe("what a finding rests on", () => {
  it("attests a record with a sensor-counted basis, whatever else it carries", () => {
    expect(sensorAttested(record({ rests_on: [SENSOR, AUTHORED] }))).toBe(true);
  });

  it("does not attest one resting only on values the adversary chose", () => {
    expect(sensorAttested(record({ rests_on: [AUTHORED] }))).toBe(false);
  });

  // Somebody else's claim about the world is nobody's observation of this estate.
  it("does not attest a third party's claim", () => {
    expect(sensorAttested(record({ rests_on: [FEED, AUTHORED] }))).toBe(false);
  });

  // A ledger written before the split has the boolean and nothing else, and has to read
  // exactly as it did.
  it("falls back to the record-level flag where no basis was named", () => {
    expect(sensorAttested(record({ attacker_influenceable: true }))).toBe(false);
    expect(sensorAttested(record({ attacker_influenceable: false }))).toBe(true);
  });
});

describe("what a verdict may rest on", () => {
  it("stops blocking a claim whose support the telemetry attested", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = hypothesisIds[0]!;
    // The shape of the real run: the adversary picked the destination and the interval,
    // the sensor counted the connections.
    evidenceOn(ledger, hypothesisId, { source: "net_flow", attackerInfluenceable: true, restsOn: [SENSOR, AUTHORED] });
    evidenceOn(ledger, hypothesisId, { source: "endpoint", attackerInfluenceable: true, restsOn: [{ field: "parent_process", authored: "sensor" }] });

    const strength = evidenceStrength(ledger.projection, hypothesisId);

    expect(strength.attacker_influenceable_only).toBe(false);
    expect(strength.corroborating_sources).toBe(2);
    expect(unmetPredicates(strength, DEFAULT_VERDICTS).filter((why) => why.includes("attested"))).toEqual([]);
  });

  it("still blocks a claim resting only on what the adversary wrote", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = hypothesisIds[0]!;
    evidenceOn(ledger, hypothesisId, { source: "http", attackerInfluenceable: true, restsOn: [AUTHORED] });
    evidenceOn(ledger, hypothesisId, { source: "intel", attackerInfluenceable: true, restsOn: [FEED] });

    const strength = evidenceStrength(ledger.projection, hypothesisId);

    expect(strength.attacker_influenceable_only).toBe(true);
    expect(unmetPredicates(strength, DEFAULT_VERDICTS).join(" ")).toMatch(/attested/);
  });
});

// The gate this flag was built for, and the one direction it was always right in: an
// adversary must not be able to talk the hunt out of a branch by writing a benign value.
describe("what an ABANDON may rest on", () => {
  // On the agent's abandon_hypothesis tool, through a whole run: record, then cite it.
  const abandonCiting = async (restsOn: unknown[]) => {
    const state = new InProcessState<HuntKinds>();
    const provider = modelOf([
      (request) => ({
        tool: "record_evidence",
        args: {
          results: [
            {
              source_system: "network",
              summary: "the beacon's filename looks like an updater",
              salience: "notable",
              why_notable: "a benign-looking value",
              payload: "{}",
              attacker_influenceable: true,
              rests_on: restsOn,
              supports: [hypothesisIn(request)],
            },
          ],
        },
      }),
      (request) => ({ tool: "abandon_hypothesis", args: { hypothesis_id: hypothesisIn(request), reason: "looks benign", evidence_ids: [evidenceIn(request)] } }),
      () => ({ say: "done" }),
    ]);
    await runHunt(harnessOf(SPEC, provider, state), { run_id: RUN, run_kind: "hunt", spec: SPEC, actions: [], queue: new InProcessDirectiveQueue() });
    const events = (await state.read(RUN)) as readonly HuntEvent[];
    const call = events.find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "abandon_hypothesis");
    return { call: call?.payload as { ok: boolean; result: string }, hypothesis: [...fold(events).hypotheses.values()][0]! };
  };

  it("refuses a drop citing only values the adversary chose", async () => {
    const { call, hypothesis } = await abandonCiting([AUTHORED]);

    expect(call.ok).toBe(false);
    expect(call.result).toContain("attested");
    // Never parked by the abandon: the run ending closes it inconclusive instead.
    expect(hypothesis.resolution_reason ?? "").not.toMatch(/^abandoned/);
  });

  it("allows one citing something the telemetry attested", async () => {
    const { call, hypothesis } = await abandonCiting([SENSOR]);

    expect(call.ok).toBe(true);
    expect(hypothesis.status).toBe("parked");
    expect(hypothesis.resolution_reason).toMatch(/^abandoned: looks benign/);
  });
});

describe("reading a worker's emission", () => {
  const emit = (rests: unknown) =>
    evidenceFrom({ results: [{ source_system: "net_flow", summary: "s", why_notable: "w", salience: "anomalous", payload: "{}", rests_on: rests }] })[0]!;

  it("carries a basis the worker named", () => {
    expect(emit([SENSOR, AUTHORED]).rests_on).toEqual([SENSOR, AUTHORED]);
  });

  // sensorAttested reads this to decide whether a claim can carry a verdict, so an
  // authorship this side does not know must never read as an attestation.
  it("drops an entry naming an authorship it does not know", () => {
    expect(emit([{ field: "x", authored: "vendor" }, SENSOR]).rests_on).toEqual([SENSOR]);
  });

  it("drops an entry naming no field", () => {
    expect(emit([{ field: "  ", authored: "sensor" }]).rests_on).toBeUndefined();
  });

  it("leaves the basis absent when the worker named none", () => {
    expect(emit(undefined).rests_on).toBeUndefined();
    expect(emit("sensor").rests_on).toBeUndefined();
  });
});
