import type { DecisionProvider, DisconfirmationCritic, WorkerDispatcher } from "../../workflows/hunt/ports.js";
import type {
  Decision,
  DecisionResult,
  Digest,
  DispatchRequest,
  DispatchResult,
  NullCheckInput,
  NullCheckResult,
} from "../../workflows/hunt/types.js";

export const SCRIPTED_MODEL_ID = "scripted";

// A scripted decision may be written as a function of the digest, so a script
// can cite evidence ids that did not exist when it was written.
export type ScriptedDecision = Decision | ((digest: Digest) => Decision);

// Replays a fixed sequence, then concludes. The fallback stops a hunt that
// outlives its script from looping forever.
export class ScriptedDecisionProvider implements DecisionProvider {
  readonly seenDigests: Digest[] = [];
  private readonly decisions: ScriptedDecision[];

  constructor(
    decisions: Iterable<ScriptedDecision>,
    private readonly costPerDecision = 0,
    private readonly modelId = SCRIPTED_MODEL_ID,
  ) {
    this.decisions = [...decisions];
  }

  get exhausted(): boolean {
    return this.decisions.length === 0;
  }

  async decide(digest: Digest): Promise<DecisionResult> {
    this.seenDigests.push(digest);
    const next = this.decisions.shift() ?? {
      action: "CONCLUDE" as const,
      rationale: "scripted provider exhausted",
    };
    const decision = typeof next === "function" ? next(digest) : next;
    return {
      decision,
      model_id: this.modelId,
      prompt_version: "scripted/v0",
      cost_usd: this.costPerDecision,
    };
  }
}

export class ScriptedWorkerDispatcher implements WorkerDispatcher {
  readonly requests: DispatchRequest[] = [];
  private readonly failAgentIds: Set<string>;

  constructor(
    private readonly evidence: DispatchResult["evidence"] = [],
    failAgentIds: Iterable<string> = [],
    private readonly costPerDispatch = 0,
  ) {
    this.failAgentIds = new Set(failAgentIds);
  }

  async dispatch(request: DispatchRequest): Promise<DispatchResult> {
    this.requests.push(request);
    if (this.failAgentIds.has(request.agent_id)) {
      return {
        dispatch_id: request.dispatch_id,
        evidence: [],
        failed: true,
        failure_reason: `scripted failure for ${request.agent_id}`,
        // A worker that failed still ran, so the scripted one bills like it did.
        cost_usd: this.costPerDispatch,
      };
    }
    return {
      dispatch_id: request.dispatch_id,
      evidence: structuredClone(this.evidence),
      failed: false,
      failure_reason: "",
      cost_usd: this.costPerDispatch,
    };
  }
}

// Argues whichever way the test needs. `survives` is about the hypothesis: false
// means the benign explanation accounted for the evidence.
export class ScriptedDisconfirmationCritic implements DisconfirmationCritic {
  readonly checks: NullCheckInput[] = [];

  constructor(
    private readonly survives = true,
    private readonly costPerCheck = 0,
    private readonly explanation = "routine administrative activity would produce the same records",
  ) {}

  async argueNull(check: NullCheckInput): Promise<NullCheckResult> {
    this.checks.push(check);
    return {
      survives: this.survives,
      strongest_benign_explanation: this.explanation,
      rationale: this.survives
        ? "the benign explanation does not account for the linked evidence"
        : "the benign explanation accounts for the linked evidence",
      cost_usd: this.costPerCheck,
      model_id: SCRIPTED_MODEL_ID,
      prompt_version: "scripted/v0",
    };
  }
}
