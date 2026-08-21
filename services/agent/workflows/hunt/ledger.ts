import type { AgentEvent, CheckpointPayload, PatchPayload, ResolutionPayload } from "../../contracts/events.js";
import type { Narrative } from "./narrative.js";
import type { State } from "../../core/seams.js";
import type {
  DecisionRecord,
  Directive,
  DispatchRecord,
  EvidenceLink,
  EvidenceRecord,
  Handoff,
  HuntState,
  Hypothesis,
  OpenQuestion,
} from "./types.js";

// What the hunt adds to the domain-free kinds. dispatch, checkpoint, resolution,
// directive, patch and terminal are the harness's and are not restated here.
export type HuntKinds = {
  hypothesis: Hypothesis;
  question: OpenQuestion;
  evidence: EvidenceRecord;
  link: EvidenceLink;
  decision: DecisionRecord;
  handoff: Handoff;
  // The report is a deliverable the fold ignores, so its shape is the report
  // builder's to own rather than the ledger's.
  finalize: unknown;
  // Written at the end and again on request: the latest wins, the earlier ones stay.
  narrative: Narrative;
};

export type HuntEvent = AgentEvent<HuntKinds>;

export class LedgerError extends Error {}

export interface Projection {
  hunt: HuntState;
  hypotheses: Map<string, Hypothesis>;
  questions: Map<string, OpenQuestion>;
  evidence: Map<string, EvidenceRecord>;
  links: EvidenceLink[];
  dispatches: Map<string, DispatchRecord>;
  decisions: DecisionRecord[];
  directives: Directive[];
  checkpoints: Map<string, CheckpointPayload>;
  resolutions: ResolutionPayload[];
  handoffs: Handoff[];
}

function opened(hunt: HuntState): Projection {
  return {
    hunt: structuredClone(hunt),
    hypotheses: new Map(),
    questions: new Map(),
    evidence: new Map(),
    links: [],
    dispatches: new Map(),
    decisions: [],
    directives: [],
    checkpoints: new Map(),
    resolutions: [],
    handoffs: [],
  };
}

// Patched in place rather than replaced, so a field the patch does not name keeps
// the value the record already held.
function applyPatch(view: Projection, payload: PatchPayload, seq: number): void {
  const target =
    payload.target === "hunt"
      ? view.hunt
      : payload.target === "hypothesis"
        ? view.hypotheses.get(payload.id)
        : payload.target === "question"
          ? view.questions.get(payload.id)
          : view.dispatches.get(payload.id);

  if (target === undefined) {
    throw new LedgerError(`patch at seq ${seq} targets unknown ${payload.target} ${payload.id}`);
  }
  Object.assign(target, payload.fields);
}

function exhaustive(event: never): never {
  throw new LedgerError(`no fold arm for ${JSON.stringify(event)}`);
}

export function fold(events: readonly HuntEvent[]): Projection {
  const first = events[0];
  if (first === undefined || first.kind !== "run") {
    throw new LedgerError("ledger does not open with a run event");
  }

  const view = opened((first.payload as unknown as { hunt: HuntState }).hunt);
  for (const event of events.slice(1)) {
    switch (event.kind) {
      case "run":
        throw new LedgerError(`second run event at seq ${event.seq}`);
      case "hypothesis":
        view.hypotheses.set(event.payload.hypothesis_id, structuredClone(event.payload));
        break;
      case "question":
        view.questions.set(event.payload.question_id, structuredClone(event.payload));
        break;
      case "evidence":
        view.evidence.set(event.payload.evidence_id, structuredClone(event.payload));
        break;
      // Upserted, not appended: the lead re-rules on every observation each iteration,
      // and evidenceStrength counting duplicate and self-contradicting links is corrupt,
      // not thorough. The latest ruling is the belief; the ledger holds the earlier ones.
      case "link": {
        const link = structuredClone(event.payload);
        const at = view.links.findIndex(
          (held) => held.evidence_id === link.evidence_id && held.hypothesis_id === link.hypothesis_id,
        );
        if (at === -1) view.links.push(link);
        else view.links[at] = link;
        break;
      }
      case "dispatch":
        view.dispatches.set(
          (event.payload as unknown as DispatchRecord).dispatch_id,
          structuredClone(event.payload) as unknown as DispatchRecord,
        );
        break;
      case "decision":
        view.decisions.push(structuredClone(event.payload));
        break;
      case "directive":
        view.directives.push(structuredClone(event.payload) as unknown as Directive);
        break;
      case "checkpoint":
        view.checkpoints.set(event.payload.checkpoint_id, structuredClone(event.payload));
        break;
      case "resolution":
        view.resolutions.push(structuredClone(event.payload));
        break;
      case "handoff":
        view.handoffs.push(structuredClone(event.payload));
        break;
      case "terminal":
        Object.assign(view.hunt, { status: "terminal", outcome: event.payload.outcome });
        break;
      case "patch":
        applyPatch(view, event.payload, event.seq);
        break;
      // Where the run was picked back up, which is the worker's history and not
      // the hunt's: no belief moved because a process restarted.
      case "resumed":
        break;
      case "finalize":
      case "spend":
      case "narrative":
        // None is state: the narrative is an account of the fold, so it cannot also be
        // an input to it.
        break;
      default:
        // A kind added without a fold arm is a silent hole in the projection, so
        // it fails to compile rather than folding to nothing.
        return exhaustive(event);
    }
  }
  return view;
}

export async function projectionOf(state: State<HuntKinds>, runId: string): Promise<Projection> {
  return fold(await state.read(runId));
}
