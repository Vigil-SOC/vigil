import { GatewayExhausted } from "../../core/limiter.js";
import type { Budget } from "../../contracts/budget.js";
import { AUTO_ACTOR, DEFAULT_CHECKPOINTS, pendingCheckpoints, raiseCheckpoint, resolutionOf, resolveCheckpoint, type Checkpoint, type CheckpointClass, type Checkpoints, type Resolution } from "../../workflows/hunt/checkpoints.js";
import { DEFAULT_TERMINATION, verdictsOf, type Termination, type Verdicts } from "../../workflows/hunt/config.js";
import { entitiesOf, fromText, key } from "../../workflows/hunt/entities.js";
import { newId } from "../../workflows/hunt/ids.js";
import { drain, grantOf, journalNote } from "../../workflows/hunt/inbox.js";
import type { Journal } from "../../workflows/hunt/journal.js";
import { boundBy, boundReason, standingOf, TOOL_FAILURE } from "../../workflows/hunt/opening.js";
import { buildReport, renderCaseFile } from "../../workflows/hunt/report.js";
import { criticPayload, sanitize, sanitizeQuestion } from "../../workflows/hunt/sanitize.js";
import { evidenceStrength, isGap, OPERATOR_GAP_PROVENANCE, recordsFailedQuery, unmetPredicates, UNDECLARED_SOURCE, NULL_CHECK_PROVENANCE, CRITIC_SOURCE_SYSTEM } from "../../workflows/hunt/strength.js";
import { terminationVerdict, type TerminationVerdict } from "../../workflows/hunt/termination.js";
import {
  OUTCOME_PRECEDENCE,
  type Budgets,
  type Directive,
  type EvidenceRecord,
  type Handoff,
  type Hypothesis,
  type HuntOutcome,
  type LinkRelation,
  type NullCheckEvidence,
  type NullCheckInput,
  type NullCheckResult,
  type OpenQuestion,
  type WorkerEvidence,
} from "../../workflows/hunt/types.js";

// The hunt's record, kept by one agent through tools. Every write is one the controller
// made, so every fold over a hunt ledger reads a kernel hunt exactly as a controller one.

// A hypothesis the agent proposed mid-hunt rather than one the hunt opened on.
export const AGENT_HYPOTHESIS_PROVENANCE = "agent";

// Prefixes the park a raised checkpoint sets, so lifting one can never lift the
// budget park, which takes a different set of answers entirely.
const AWAITING = "awaiting ";

// Why a hypothesis could not be settled when the cause is what the hunt could not see.
export function couldNotFinish(gaps: number): string {
  return `could not finish: ${gaps} source${gaps === 1 ? "" : "s"} unreachable`;
}

export class HuntBook {
  constructor(
    readonly ledger: Journal,
    // The harness's own ceiling, so an extension can widen what the pool refuses on.
    private readonly pool?: Budget,
  ) {}

  get projection() {
    return this.ledger.projection;
  }

  get hunt() {
    return this.ledger.projection.hunt;
  }

  // The deployment's own thresholds, off the journaled spec: a resumed hunt keeps
  // the bar it started under.
  private get verdicts(): Verdicts {
    return verdictsOf(this.hunt.spec);
  }

  private get termination(): Termination {
    return { ...DEFAULT_TERMINATION, ...(this.hunt.spec.termination ?? {}) };
  }

  private get checkpoints(): Checkpoints {
    return { ...DEFAULT_CHECKPOINTS, ...(this.hunt.spec.checkpoints ?? {}) };
  }

  ended(): boolean {
    return this.hunt.status === "terminal";
  }

  // --- the turn ---------------------------------------------------------------

  // A model turn that called tools is an iteration: the budget's turn count and the
  // report's "at iteration n" read this, as they read the controller's.
  advanceTo(iteration: number): void {
    if (iteration > this.hunt.iteration && !this.ended()) this.ledger.patch("hunt", this.hunt.hunt_id, { iteration });
  }

  // --- evidence -----------------------------------------------------------------

  // Every evidence source reaches the ledger through sanitize() and entity
  // extraction, whoever wrote it.
  appendEvidence(records: readonly WorkerEvidence[], dispatchId: string | null = null): EvidenceRecord[] {
    return records.map(sanitize).map(({ supports, weakens, query_failed, ...observed }) => {
      const evidenceId = newId("ev");
      // A row recording a query that failed or came back empty is a blind spot (#2207),
      // filed the way a failed call is and never linked: it bears on no ruling.
      const gap = observed.provenance === "worker" && recordsFailedQuery({ ...observed, query_failed });
      const record: WorkerEvidence = gap
        ? {
            ...observed,
            provenance: TOOL_FAILURE,
            payload: { ...observed.payload, tool: observed.source_system, kind: "empty", hypothesis_id: this.servedBy(supports, weakens) },
            why_notable: "a blind spot in what this run could see, not a finding",
          }
        : observed;
      const stored: EvidenceRecord = {
        ...record,
        ...this.attributeSource(record),
        evidence_id: evidenceId,
        dispatch_id: dispatchId,
        iteration: this.hunt.iteration,
        // A failed call's text is ours, not the estate's: its addresses are the
        // gateway, and a hunt must not investigate its own plumbing.
        entities: record.provenance === TOOL_FAILURE ? [] : entitiesOf(record),
        captured_at: new Date().toISOString(),
      };
      this.ledger.append({ kind: "evidence", payload: stored });
      if (!gap) this.link(evidenceId, supports, weakens);
      return stored;
    });
  }

  // The hypothesis a failed query was run for: the one known id the row names, as a
  // dispatch served one. Naming none or several, it bears on every claim.
  private servedBy(supports?: string[], weakens?: string[]): string | null {
    const named = [...new Set([...(supports ?? []), ...(weakens ?? [])])].filter((id) => this.projection.hypotheses.has(id));
    return named.length === 1 ? named[0]! : null;
  }

  // A blind spot: a call the estate could not answer. openGaps counts it once per tool,
  // on the hypothesis it names or every claim, so it gap-locks as a failed dispatch did.
  appendGap(tool: string, kind: "timeout" | "unavailable" | "backend_error", hypothesisId: string | null): void {
    this.appendEvidence([
      {
        source_system: "dispatcher",
        summary: "a query the hunt wanted could not be run",
        payload: { tool, kind, gap_key: `${tool}:${kind}:${hypothesisId ?? "-"}`, hypothesis_id: hypothesisId },
        salience: "routine",
        why_notable: "a blind spot in what this run could see, not a finding",
        provenance: TOOL_FAILURE,
        attacker_influenceable: false,
        instruction_like: false,
      },
    ]);
  }

  // Corroboration is counted over source systems, so a label the hunt never
  // declared earns no independence credit: it collapses into one bucket.
  private attributeSource(record: WorkerEvidence): Pick<EvidenceRecord, "source_system" | "payload"> {
    const declared = this.hunt.spec.data_domains;
    if (record.provenance !== "worker" || declared.length === 0 || declared.includes(record.source_system)) {
      return { source_system: record.source_system, payload: record.payload };
    }
    return { source_system: UNDECLARED_SOURCE, payload: { ...record.payload, claimed_source_system: record.source_system } };
  }

  // Only ids the ledger already knows are linked: a link to an invented hypothesis
  // would corrupt the contrarian quota.
  private link(evidenceId: string, supports?: string[], weakens?: string[]): void {
    const known = this.projection.hypotheses;
    for (const [relation, ids] of [["supports", supports], ["weakens", weakens]] as const) {
      for (const hypothesisId of ids ?? []) {
        if (!known.has(hypothesisId)) continue;
        this.ledger.append({ kind: "link", payload: { evidence_id: evidenceId, hypothesis_id: hypothesisId, relation } });
      }
    }
  }

  // One observation against a hypothesis, "neither" included: the ruling is what
  // makes the update auditable. A neither never undoes a supports or weakens.
  rule(relations: readonly { evidence_id: string; hypothesis_id: string; relation: LinkRelation }[]): { ruled: number; skipped: string[] } {
    const skipped: string[] = [];
    let ruled = 0;
    for (const relation of relations) {
      const record = this.projection.evidence.get(relation.evidence_id);
      if (record === undefined) {
        skipped.push(`${relation.evidence_id} is not on the ledger`);
        continue;
      }
      if (!this.projection.hypotheses.has(relation.hypothesis_id)) {
        skipped.push(`${relation.hypothesis_id} is not on the ledger`);
        continue;
      }
      const decided = this.projection.links.some(
        (link) => link.evidence_id === relation.evidence_id && link.hypothesis_id === relation.hypothesis_id && link.relation !== "neither",
      );
      if (relation.relation === "neither" && decided) continue;
      // A gap is not evidence: not having looked is no reason to believe a claim and
      // none to doubt it, so only neither is taken on a blind spot (#2207).
      if (relation.relation !== "neither" && isGap(record)) {
        skipped.push(
          `${relation.evidence_id} records a query that failed or returned nothing, so it is a blind spot ` +
            `and cannot be ruled ${relation.relation} against ${relation.hypothesis_id}; rule it neither`,
        );
        continue;
      }
      this.ledger.append({ kind: "link", payload: { ...relation } });
      ruled += 1;
    }
    return { ruled, skipped };
  }

  // A claim the agent put on the board itself, a peer of the playbook's own.
  propose(statement: string): string {
    const hypothesisId = newId("h", 4);
    this.ledger.append({
      kind: "hypothesis",
      payload: {
        hypothesis_id: hypothesisId,
        statement: sanitizeQuestion(statement),
        status: "active",
        attack_technique: null,
        provenance: AGENT_HYPOTHESIS_PROVENANCE,
        resolution_reason: null,
        evidence_strength: null,
      },
    });
    return hypothesisId;
  }

  // --- questions ----------------------------------------------------------------

  raise(question: string, provenance: Partial<Omit<OpenQuestion, "question_id" | "question" | "status">>): string {
    const questionId = newId("q", 4);
    this.ledger.append({
      kind: "question",
      payload: {
        question_id: questionId,
        question: sanitizeQuestion(question),
        status: "open",
        entity_key: null,
        spawning_evidence_id: null,
        spawning_dispatch_id: null,
        spawned_iteration: this.hunt.iteration,
        hypothesis_id: null,
        ...provenance,
      },
    });
    return questionId;
  }

  close(questionId: string, reason: string): string | null {
    const question = this.projection.questions.get(questionId);
    if (question === undefined) return `${questionId} is not on the ledger`;
    if (question.status !== "open") return `${questionId} is already ${question.status}`;
    this.ledger.patch("question", questionId, { status: "closed", closed_reason: reason });
    return null;
  }

  // --- verdicts -----------------------------------------------------------------

  // Raw payloads, not summaries: the critic argues against what was collected.
  nullCheckInput(hypothesis: Hypothesis): NullCheckInput {
    const evidence = this.projection.links
      .filter((link) => link.hypothesis_id === hypothesis.hypothesis_id)
      .map((link) => ({ relation: link.relation, record: this.projection.evidence.get(link.evidence_id) }))
      .filter((linked): linked is NullCheckEvidence => linked.record !== undefined);
    return { hypothesis_id: hypothesis.hypothesis_id, statement: hypothesis.statement, narrative: this.hunt.narrative, evidence };
  }

  // The one writer of proven. Returns what to tell the agent, and whether the
  // hunt now waits on a person.
  applyVerdict(hypothesisId: string, nullCheck: NullCheckResult, argued: readonly string[]): { said: string; parked?: string } {
    const [record] = this.appendEvidence([
      {
        source_system: CRITIC_SOURCE_SYSTEM,
        summary: `strongest benign explanation: ${nullCheck.strongest_benign_explanation}`,
        payload: criticPayload({
          hypothesis_id: hypothesisId,
          survives: nullCheck.survives,
          argued_evidence_ids: [...argued],
          strongest_benign_explanation: nullCheck.strongest_benign_explanation,
          rationale: nullCheck.rationale,
          model_id: nullCheck.model_id,
          prompt_version: nullCheck.prompt_version,
          cost_usd: nullCheck.cost_usd,
        }),
        salience: "notable",
        why_notable: nullCheck.rationale,
        provenance: NULL_CHECK_PROVENANCE,
        attacker_influenceable: false,
        instruction_like: false,
        ...(nullCheck.survives ? {} : { weakens: [hypothesisId] }),
      },
    ]);
    if (record === undefined) return { said: `${hypothesisId}: the critic's argument could not be recorded` };

    const strength = evidenceStrength(this.projection, hypothesisId);
    // Whichever way the critic argued, not having looked never proves. Status is earned by
    // rulings, so the hypothesis keeps it and says why it could not finish (#2207).
    if (strength.open_gaps >= this.verdicts.gap_lock_threshold) {
      this.ledger.patch("hypothesis", hypothesisId, {
        resolution_reason: couldNotFinish(strength.open_gaps),
        evidence_strength: strength,
      });
      return { said: `${hypothesisId} not proven (gap-locked): ${couldNotFinish(strength.open_gaps)}` };
    }

    const unmet = unmetPredicates(strength, this.verdicts, { projection: this.projection, hypothesisId });
    if (unmet.length > 0) return { said: `${hypothesisId} stays active: ${unmet.join("; ")}` };

    const patch = {
      status: "proven",
      resolution_reason:
        `survived the argue-the-null pass against "${nullCheck.strongest_benign_explanation}" ` +
        `on ${strength.corroborating_sources} corroborating source system(s)`,
      evidence_strength: strength,
    };
    const hypothesis = this.projection.hypotheses.get(hypothesisId);
    const { checkpoint, parked } = this.ask(
      "verdict_review",
      `Mark ${hypothesisId} proven? ${hypothesis?.statement ?? ""}`.trim(),
      { kind: "hypothesis", hypothesis_id: hypothesisId, patch, evidence_strength: strength },
    );
    if (parked) return { said: `${hypothesisId} awaits verdict review at checkpoint ${checkpoint.checkpoint_id}`, parked: checkpoint.checkpoint_id };
    this.resolve(checkpoint, "approve", AUTO_ACTOR, "checkpoint policy verdict_review=auto");
    this.ledger.patch("hypothesis", hypothesisId, patch);
    return { said: `${hypothesisId} proven` };
  }

  // Parked rather than disproven: the hunt stopped looking, which is not the same
  // as having cleared the branch.
  abandon(hypothesisId: string, reason: string, cited: readonly string[]): string {
    const hypothesis = this.projection.hypotheses.get(hypothesisId);
    if (hypothesis === undefined) return `${hypothesisId} is not on the ledger`;
    if (hypothesis.status !== "active") return `${hypothesisId} is already ${hypothesis.status}`;
    this.ledger.patch("hypothesis", hypothesisId, {
      status: "parked",
      resolution_reason: `abandoned: ${reason} [${cited.join(", ")}]`,
    });
    return `${hypothesisId} parked`;
  }

  // An escalation rests on a verdict: anything not proven is refused.
  handoff(hypothesisId: string, rationale: string): { said: string; refused: boolean } {
    const hypothesis = this.projection.hypotheses.get(hypothesisId);
    if (hypothesis === undefined || hypothesis.status !== "proven") {
      const state = hypothesis === undefined ? "not on the ledger" : hypothesis.status;
      return { said: `refused: ${hypothesisId || "no hypothesis named"} is ${state}, not proven; validate it first`, refused: true };
    }
    const caseId = newId("case", 4);
    this.ledger.patch("hypothesis", hypothesisId, { status: "handed_off", spawned_case_id: caseId });
    const handoff: Handoff = { case_id: caseId, hypothesis_id: hypothesisId, iteration: this.hunt.iteration, rationale, created_at: new Date().toISOString() };
    handoff.case_markdown = renderCaseFile(this.projection, handoff);
    this.ledger.append({ kind: "handoff", payload: handoff });
    return { said: `${hypothesisId} handed off to incident response as ${caseId}`, refused: false };
  }

  // --- the person in the loop -------------------------------------------------

  // The agent asking for an adult: under "ask" the hunt stops until someone
  // answers, under "auto" the concern is journaled and the hunt carries on.
  askOperator(question: string, hypothesisId: string | null): { said: string; parked?: string } {
    const { checkpoint, parked } = this.ask("budget_anomaly", question, { raised_by: "hunt_agent", target_hypothesis_id: hypothesisId });
    if (parked) return { said: `parked on checkpoint ${checkpoint.checkpoint_id}`, parked: checkpoint.checkpoint_id };
    this.resolve(checkpoint, "approve", AUTO_ACTOR, "checkpoint policy budget_anomaly=auto");
    journalNote(this.ledger, `The hunt agent raised a concern: ${question}. Checkpoint policy is auto, so nobody was asked and the hunt continued.`);
    return { said: `checkpoint ${checkpoint.checkpoint_id} journaled; policy is auto, so the hunt continues` };
  }

  private ask(checkpointClass: CheckpointClass, question: string, context: Record<string, unknown>): { checkpoint: Checkpoint; parked: boolean } {
    const checkpoint = raiseCheckpoint(checkpointClass, this.hunt.iteration, question, context);
    this.ledger.append({ kind: "checkpoint", payload: checkpoint });
    if (this.checkpoints[checkpointClass] !== "ask") return { checkpoint, parked: false };
    this.ledger.patch("hunt", this.hunt.hunt_id, {
      status: "parked",
      parked_at: new Date().toISOString(),
      parked_reason: `${AWAITING}${checkpointClass} checkpoint ${checkpoint.checkpoint_id}: ${question}`,
    });
    return { checkpoint, parked: true };
  }

  private resolve(checkpoint: Checkpoint, answer: Resolution["answer"], actor: string, text: string, directive: Directive | null = null): void {
    this.ledger.append({ kind: "resolution", payload: resolveCheckpoint(checkpoint, answer, actor, text, directive) });
  }

  // Lifted only when nothing is left to answer, and never the budget park.
  private unpark(): void {
    const hunt = this.hunt;
    if (hunt.status !== "parked" && hunt.status !== "pending_approval") return;
    if (hunt.status === "parked" && !(hunt.parked_reason ?? "").startsWith(AWAITING)) return;
    if (pendingCheckpoints(this.projection).length > 0) return;
    this.ledger.patch("hunt", hunt.hunt_id, { status: "active", parked_at: null, parked_reason: null });
  }

  // Answers on the ledger, from the console or a directive below, applied once each:
  // idempotent on what the ledger already says, so a resume applies nothing twice.
  reconcile(): void {
    for (const checkpoint of this.projection.checkpoints.values()) {
      const answer = resolutionOf(this.projection, checkpoint.checkpoint_id);
      if (answer === undefined || answer.actor === AUTO_ACTOR) continue;
      this.applyAnswer(checkpoint, answer.answer === "approve", answer.actor, answer.text);
      if (this.ended()) return;
    }
    this.unpark();
  }

  private applyAnswer(checkpoint: Checkpoint, approved: boolean, actor: string, text: string): void {
    const context = checkpoint.context ?? {};
    switch (checkpoint.checkpoint_class) {
      case "hypothesis_approval":
        if (this.hunt.status !== "pending_approval") return;
        if (approved) this.unpark();
        else this.terminate("aborted", `${actor} rejected the hypotheses at the start checkpoint: ${text}`);
        return;
      case "verdict_review": {
        if (context["kind"] === "conclude") {
          if (!approved || this.ended()) return;
          for (const questionId of (context["park"] as string[] | undefined) ?? []) this.parkQuestion(questionId);
          this.terminate(context["outcome"] as HuntOutcome, `${actor} approved the conclusion at checkpoint ${checkpoint.checkpoint_id}`);
          return;
        }
        const hypothesisId = String(context["hypothesis_id"] ?? "");
        if (!approved || this.projection.hypotheses.get(hypothesisId)?.status !== "active") return;
        this.applyReviewed(checkpoint, actor, hypothesisId);
        return;
      }
      case "scope_extension": {
        if (!approved) return;
        const entityKey = (context["entity_key"] as string | null) ?? null;
        const declared = (this.hunt.scope["entities"] as string[] | undefined) ?? [];
        if (entityKey === null || declared.includes(entityKey)) return;
        this.ledger.patch("hunt", this.hunt.hunt_id, { scope: { ...this.hunt.scope, entities: [...declared, entityKey] } });
        this.raise(String(context["question"] ?? ""), { entity_key: entityKey });
        journalNote(this.ledger, `${actor} extended the hunt's scope to ${entityKey} at ${checkpoint.checkpoint_id}.`);
        return;
      }
      case "budget_anomaly":
        return;
    }
  }

  // The approved verdict, applied only if the evidence still carries it.
  private applyReviewed(checkpoint: Checkpoint, actor: string, hypothesisId: string): void {
    const strength = evidenceStrength(this.projection, hypothesisId);
    if (strength.open_gaps >= this.verdicts.gap_lock_threshold) {
      this.ledger.patch("hypothesis", hypothesisId, {
        resolution_reason: couldNotFinish(strength.open_gaps),
        evidence_strength: strength,
      });
      journalNote(
        this.ledger,
        `${actor} approved the verdict on ${hypothesisId}, but ${strength.open_gaps} open gap(s) now ` +
          "bear on it. It is not proven and keeps the status its rulings earned — the approval stands, the evidence moved.",
      );
      return;
    }
    const unmet = unmetPredicates(strength, this.verdicts, { projection: this.projection, hypothesisId });
    if (unmet.length > 0) {
      journalNote(this.ledger, `${actor} approved the verdict on ${hypothesisId}, but the evidence moved while it waited: ${unmet.join("; ")}. It stays active.`);
      return;
    }
    this.ledger.patch("hypothesis", hypothesisId, (checkpoint.context ?? {})["patch"] as Record<string, unknown>);
  }

  // Human input at the boundary, before the agent goes on. Returns true when the
  // hunt ended here.
  async applyDirectives(): Promise<boolean> {
    let conclude = false;
    for (const directive of await drain(this.ledger)) {
      if (this.ended()) break;
      switch (directive.kind) {
        case "abort":
          this.terminate("aborted", "an operator halted the hunt");
          return true;
        case "conclude":
          conclude = true;
          break;
        case "lead":
          this.applyLead(directive);
          break;
        case "extend":
          this.extend(directive);
          break;
        case "approve":
        case "reject":
          this.answerDirective(directive);
          break;
        case "gap":
          this.applyGap(directive);
          break;
        case "benign":
          journalNote(
            this.ledger,
            directive.revoke === true
              ? `${directive.actor} lifted the known-benign suppression on ${directive.entity_key ?? directive.text}.`
              : `${directive.actor} marked ${directive.entity_key ?? directive.text} known-benign. Its evidence stands; open no new work on it.`,
          );
          break;
        case "boost":
          journalNote(this.ledger, `${directive.actor} asked for ${directive.question_id ?? directive.text} to be taken next.`);
          break;
        case "note":
          break;
      }
    }
    if (this.ended()) return true;
    if (conclude) {
      const bound = boundBy(this.hunt);
      if (bound !== null) this.terminate("budget_terminated", "an operator accepted the stop at the budget checkpoint");
      else this.terminate("completed", `an operator asked the hunt to conclude on what it had: ${standingOf(this.hunt)}`);
      return true;
    }
    return this.expireParked();
  }

  private answerDirective(directive: Directive): void {
    const checkpoint = this.projection.checkpoints.get(directive.checkpoint_id ?? "");
    if (checkpoint === undefined) {
      journalNote(this.ledger, `${directive.actor} answered checkpoint ${directive.checkpoint_id ?? "(none named)"}, which this hunt never raised.`);
      return;
    }
    const settled = resolutionOf(this.projection, checkpoint.checkpoint_id);
    if (settled !== undefined) {
      journalNote(this.ledger, `${directive.actor} answered ${checkpoint.checkpoint_id}, already ${settled.answer} by ${settled.actor}. The first answer stands.`);
      return;
    }
    this.resolve(checkpoint, directive.kind === "approve" ? "approve" : "reject", directive.actor, directive.text, directive);
    this.applyAnswer(checkpoint, directive.kind === "approve", directive.actor, directive.text);
    this.unpark();
  }

  // A lead grows what the hunt looks at, so scope is consulted. A tenant crossing
  // is refused outright; an entity outside a declared scope asks first.
  private applyLead(directive: Directive): void {
    const declaredTenant = this.hunt.scope["tenant"];
    const named = directive.tenant ?? /\btenant[:=]\s*([^\s,;]+)/i.exec(directive.text)?.[1];
    if (typeof declaredTenant === "string" && declaredTenant !== "" && named !== undefined && named.toLowerCase() !== declaredTenant.toLowerCase()) {
      journalNote(this.ledger, `${directive.actor}'s lead names tenant ${named}, and this hunt is scoped to ${declaredTenant}. Refused; start a hunt in that tenant instead.`);
      return;
    }
    const entity = directive.entity_key ?? fromText(directive.text).map(key)[0] ?? null;
    const declared = new Set((this.hunt.scope["entities"] as string[] | undefined) ?? []);
    const outside = entity !== null && declared.size > 0 && !declared.has(entity) ? entity : null;
    if (outside !== null) {
      const { checkpoint, parked } = this.ask("scope_extension", `Extend this hunt's scope to ${outside}?`, { question: directive.text, entity_key: outside, actor: directive.actor });
      if (parked) return;
      this.resolve(checkpoint, "approve", AUTO_ACTOR, "checkpoint policy scope_extension=auto");
      this.ledger.patch("hunt", this.hunt.hunt_id, { scope: { ...this.hunt.scope, entities: [...declared, outside] } });
    }
    this.raise(directive.text, { entity_key: directive.entity_key ?? outside ?? null });
  }

  private applyGap(directive: Directive): void {
    const hypothesisId = directive.hypothesis_id !== undefined && this.projection.hypotheses.has(directive.hypothesis_id) ? directive.hypothesis_id : null;
    this.appendEvidence([
      {
        source_system: "operator",
        summary: `operator-declared visibility gap: ${directive.text}`,
        payload: { hypothesis_id: hypothesisId, actor: directive.actor, directive_id: directive.directive_id },
        salience: "notable",
        why_notable: "the hunt cannot see here, and no query will tell it so",
        provenance: OPERATOR_GAP_PROVENANCE,
        attacker_influenceable: false,
        instruction_like: false,
      },
    ]);
  }

  // Raises the budgets, capped by the hard ceiling, and un-parks only if the grant
  // actually bought room.
  private extend(directive: Directive): void {
    const hunt = this.hunt;
    const grant = grantOf(directive);
    if (grant.iterations <= 0 && grant.cost_usd <= 0 && grant.wall_ms <= 0) {
      journalNote(this.ledger, `extend "${directive.text}" granted nothing readable; say "+5 iterations", "+$10" or "+30 minutes".`);
      return;
    }
    const { hard_max_iterations, hard_max_calls, hard_max_cost_usd, hard_max_wall_ms } = this.termination;
    const perTurn = Math.max(1, Math.round(hunt.budgets.max_calls / Math.max(1, hunt.budgets.max_iterations)));
    const iterations = Math.min(hunt.budgets.max_iterations + grant.iterations, hard_max_iterations);
    const budgets: Budgets = {
      max_iterations: iterations,
      max_calls: Math.min(iterations * perTurn, hard_max_calls),
      max_cost_usd: Math.min(Number((hunt.budgets.max_cost_usd + grant.cost_usd).toFixed(6)), hard_max_cost_usd),
      max_wall_ms: Math.min(hunt.budgets.max_wall_ms + grant.wall_ms, hard_max_wall_ms),
      max_park_ms: hunt.budgets.max_park_ms,
    };
    this.ledger.patch("hunt", hunt.hunt_id, { budgets });
    this.pool?.raise(budgets);
    const asked = {
      iterations: hunt.budgets.max_iterations + grant.iterations,
      cost_usd: Number((hunt.budgets.max_cost_usd + grant.cost_usd).toFixed(6)),
      wall_ms: hunt.budgets.max_wall_ms + grant.wall_ms,
    };
    if (budgets.max_iterations < asked.iterations || budgets.max_cost_usd < asked.cost_usd || budgets.max_wall_ms < asked.wall_ms) {
      journalNote(
        this.ledger,
        `${directive.actor}'s extension was clamped to the hard ceiling: ${budgets.max_iterations} turns, $${budgets.max_cost_usd.toFixed(2)}, ` +
          `${Math.round(budgets.max_wall_ms / 60_000)} minutes.`,
      );
    }
    if (hunt.status === "parked" && !(hunt.parked_reason ?? "").startsWith(AWAITING)) {
      if (boundBy(this.hunt) === null) this.ledger.patch("hunt", hunt.hunt_id, { status: "active", parked_at: null, parked_reason: null });
      else journalNote(this.ledger, `${directive.actor}'s extension leaves no room to run -- ${boundReason(this.hunt)} -- so the hunt stays parked.`);
    }
  }

  private expireParked(): boolean {
    const hunt = this.hunt;
    if (hunt.status !== "parked" || !hunt.parked_at) return false;
    const ttl = this.termination.park_ttl_ms;
    if (!(Date.now() - Date.parse(hunt.parked_at) >= ttl)) return false;
    this.terminate("aborted", `parked since ${hunt.parked_at} with no operator decision, past the ${Math.round(ttl / 86_400_000)}-day park TTL`);
    return true;
  }

  // Why the hunt will not step, in the words the console shows on the run row.
  suspendedBecause(): string {
    const pending = pendingCheckpoints(this.projection);
    if (pending.length > 0) return `Waiting for your approval: ${pending.map((checkpoint) => checkpoint.question).join(" | ")}`;
    return `Stopped: ${this.hunt.parked_reason ?? "awaiting an operator"}. Use Keep going on the run to carry on, or conclude it.`;
  }

  // The harness refused a call on a ceiling: the hunt parks on extend, conclude or
  // abort rather than ending, because running out of money is not a verdict.
  parkOnRefusal(reason: string): string {
    if (this.hunt.status !== "active") return this.suspendedBecause();
    this.ledger.patch("hunt", this.hunt.hunt_id, { status: "parked", parked_at: new Date().toISOString(), parked_reason: reason });
    return reason;
  }

  // --- ending -------------------------------------------------------------------

  // The agent recommends stopping; the termination predicate decides.
  conclude(reason: string): { said: string; concluded: boolean; parked?: string } {
    const verdict = terminationVerdict(this.projection, this.hunt.iteration, this.termination, this.verdicts);
    if (verdict.outcome === null) return { said: `refused: ${verdict.blocked_by}. Resolve it before concluding again.`, concluded: false };
    const { checkpoint, parked } = this.ask("verdict_review", `Conclude this hunt as ${verdict.outcome}?`, {
      kind: "conclude",
      outcome: verdict.outcome,
      park: verdict.park.map((question) => question.question_id),
      reason,
    });
    if (parked) return { said: `awaiting review of the conclusion at checkpoint ${checkpoint.checkpoint_id}`, concluded: false, parked: checkpoint.checkpoint_id };
    this.resolve(checkpoint, "approve", AUTO_ACTOR, "checkpoint policy verdict_review=auto");
    this.concludeWith(verdict, reason);
    return { said: `concluded as ${verdict.outcome}`, concluded: true };
  }

  private concludeWith(verdict: TerminationVerdict & { outcome: HuntOutcome }, reason: string): void {
    for (const question of verdict.park) this.parkQuestion(question.question_id);
    this.terminate(verdict.outcome, reason);
  }

  private parkQuestion(questionId: string): void {
    this.ledger.patch("question", questionId, {
      status: "parked",
      closed_reason: `parked to the backlog: below the priority floor of ${this.termination.priority_floor} when the hunt ended`,
    });
  }

  // Unresolved hypotheses become inconclusive, never disproven: the hunt stopped
  // looking, which is not the same as having cleared them.
  terminate(outcome: HuntOutcome, reason = ""): void {
    const hunt = this.hunt;
    if (hunt.outcome !== null && OUTCOME_PRECEDENCE[hunt.outcome] >= OUTCOME_PRECEDENCE[outcome]) return;
    for (const hypothesis of this.projection.hypotheses.values()) {
      if (hypothesis.status !== "active") continue;
      // An ending that could not look keeps the status the rulings earned and says what
      // was out of reach; overwriting it with inconclusive would read as evidence turning.
      const gaps = evidenceStrength(this.projection, hypothesis.hypothesis_id).open_gaps;
      if (gaps >= this.verdicts.gap_lock_threshold) {
        this.ledger.patch("hypothesis", hypothesis.hypothesis_id, { resolution_reason: couldNotFinish(gaps) });
        continue;
      }
      this.ledger.patch("hypothesis", hypothesis.hypothesis_id, {
        status: "inconclusive",
        resolution_reason: `hunt ended (${outcome}) with the hypothesis unresolved`,
      });
    }
    this.ledger.patch("hunt", hunt.hunt_id, {
      status: "terminal",
      outcome,
      terminated_at: new Date().toISOString(),
      ...(reason === "" ? {} : { termination_reason: reason }),
    });
    // Journaled, not written: the report is a fold over the ledger.
    this.ledger.append({ kind: "finalize", payload: buildReport(this.projection) });
  }

  // The budget's own ending, read when the harness refused: a hunt that has
  // nothing left to do concludes; otherwise it parks.
  atCeiling(reason: string): "concluded" | "parked" {
    const verdict = terminationVerdict(this.projection, this.hunt.iteration, this.termination, this.verdicts);
    if (verdict.outcome !== null) {
      this.concludeWith(verdict, `the termination predicate passed as the budget ran out: ${reason}`);
      return "concluded";
    }
    this.parkOnRefusal(boundBy(this.hunt) === null ? reason : boundReason(this.hunt));
    return "parked";
  }
}

export function isGatewayExhausted(error: unknown): boolean {
  return error instanceof GatewayExhausted;
}
