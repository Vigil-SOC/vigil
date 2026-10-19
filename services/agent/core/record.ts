import { defineTool, type RegisteredTool, type ToolResult } from "../contracts/tool.js";
import type {
  CallClass,
  CallPayload,
  HypothesisPayload,
  HypothesisStatus,
  LinkPayload,
  PatchPayload,
  QuestionPayload,
} from "../contracts/events.js";
import { isVisibilityGap } from "./security.js";

// The hypothesis record: beliefs, what bears on each, and what the run could not answer.
// Three kernel tools keep it and one fold reads it; a plugin may supply a richer backend.

export const PROPOSE_TOOL = "propose_hypothesis";
export const LINK_TOOL = "link_evidence";
export const QUESTION_TOOL = "open_question";
export const RECORD_TOOLS = [PROPOSE_TOOL, LINK_TOOL, QUESTION_TOOL] as const;

export type Stance = LinkPayload["relation"];

// What every agent that keeps a record is told about keeping it cheaply: a record
// write in a turn of its own is a model call that looked at nothing.
export const RECORD_GUIDANCE = [
  "## Keeping the record",
  "Write the record in the same turn as your next query, never in a turn of its own. link_evidence proposes a hypothesis by statement as it links.",
].join("\n");

export interface ProposedLink {
  evidence_id: string;
  hypothesis_id: string;
  relation: Stance;
  // What the result shows, in the agent's words: the observation a reader sees.
  note?: string;
}

// The benign claim is the one a dismissal would rest on, and a reader has to tell it
// from the others at a glance.
const BENIGN = {
  type: "boolean",
  description: "True for the innocent explanation: the activity is legitimate, expected or authorized.",
};

// Where the three tools write. Each answers as a tool does, so a refusal reaches
// the model as one and the call is journaled as a defect.
export interface RecordBackend {
  // What link_evidence accepts. The kernel's own takes supports and weakens.
  stances: readonly Stance[];
  // What link_evidence's evidence_id names, for the tool's description.
  evidence: string;
  propose(statement: string, benign?: boolean): Promise<ToolResult>;
  link(links: readonly ProposedLink[]): Promise<ToolResult>;
  ask(question: string, hypothesisId: string | null): Promise<ToolResult>;
}

// --- classification ----------------------------------------------------------

// Every result is one of three things, decided here and nowhere else. A gap is the
// estate not answering; a defect is the call being wrong, and is never a gap.
export function classify(result: ToolResult): CallClass {
  if (result.ok) return "served";
  return isVisibilityGap(result.failure) ? "visibility_gap" : "defect";
}

// For a call journaled before calls carried their class.
export function classOf(call: CallPayload): CallClass {
  if (call.class !== undefined) return call.class;
  if (call.ok) return "served";
  return call.failure === "timeout" || call.failure === "unavailable" || call.failure === "backend_error" ? "visibility_gap" : "defect";
}

// --- the fold ----------------------------------------------------------------

export interface RecordView {
  hypotheses: Map<string, HypothesisPayload>;
  links: LinkPayload[];
  questions: Map<string, QuestionPayload>;
}

// Patches are applied too: a plugin that moves a hypothesis's status patches it.
export function recordOf(events: readonly { kind: string; payload: unknown }[]): RecordView {
  const view: RecordView = { hypotheses: new Map(), links: [], questions: new Map() };
  for (const event of events) {
    if (event.kind === "hypothesis") {
      const payload = event.payload as HypothesisPayload;
      view.hypotheses.set(payload.hypothesis_id, { ...payload });
    } else if (event.kind === "link") {
      view.links.push(event.payload as LinkPayload);
    } else if (event.kind === "question") {
      const payload = event.payload as QuestionPayload;
      view.questions.set(payload.question_id, { ...payload });
    } else if (event.kind === "patch") {
      const patch = event.payload as PatchPayload;
      const target =
        patch.target === "hypothesis" ? view.hypotheses.get(patch.id) : patch.target === "question" ? view.questions.get(patch.id) : undefined;
      if (target !== undefined) Object.assign(target, patch.fields);
    }
  }
  return view;
}

// --- the rows a screen reads --------------------------------------------------

export interface BearsOn {
  hypothesis_id: string;
  relation: Stance;
}

export interface RecordStanding {
  hypothesis_id: string;
  statement: string;
  status: HypothesisStatus;
  supports: number;
  weakens: number;
  // The evidence rows the caller counts that take no stance on this belief.
  neither: number;
  resolution_reason: string | null;
  provenance: string;
}

export interface RecordQuestion {
  question_id: string;
  question: string;
  hypothesis_id: string | null;
}

export interface RecordRows {
  hypotheses: RecordStanding[];
  // Keyed by evidence id: evidence attached to nothing has no entry.
  bears_on: Map<string, BearsOn[]>;
  open_questions: RecordQuestion[];
}

// What any ledger that keeps a record shows a reader. `evidence` is the rows the caller
// counts neither over, which differs by plugin. A row linked to the same hypothesis twice
// was re-weighed: the later stance stands, where the first one was.
export function recordRows(
  record: { hypotheses: ReadonlyMap<string, HypothesisPayload>; links: readonly LinkPayload[]; questions: ReadonlyMap<string, QuestionPayload> },
  evidence: Iterable<string>,
): RecordRows {
  const links = [...new Map(record.links.map((link) => [`${link.evidence_id} ${link.hypothesis_id}`, link])).values()];
  const counted = [...evidence];
  const bears_on = new Map<string, BearsOn[]>();
  for (const { evidence_id, hypothesis_id, relation } of links) bears_on.set(evidence_id, [...(bears_on.get(evidence_id) ?? []), { hypothesis_id, relation }]);
  const hypotheses = [...record.hypotheses.values()].map(({ hypothesis_id, statement, status, resolution_reason, provenance }) => {
    const own = links.filter((link) => link.hypothesis_id === hypothesis_id);
    const taken = new Set(own.filter((link) => link.relation !== "neither").map((link) => link.evidence_id));
    return {
      hypothesis_id,
      statement,
      status,
      supports: own.filter((link) => link.relation === "supports").length,
      weakens: own.filter((link) => link.relation === "weakens").length,
      neither: counted.filter((id) => !taken.has(id)).length,
      resolution_reason,
      provenance,
    };
  });
  const open_questions = [...record.questions.values()]
    .filter((question) => question.status === "open")
    .map(({ question_id, question, hypothesis_id }) => ({ question_id, question, hypothesis_id }));
  return { hypotheses, bears_on, open_questions };
}

export interface VisibilityGap {
  ref: string;
  tool: string;
  failure: string | null;
}

// What the run could not answer, declared: questions never closed, what it said it could
// not settle, and every call the estate failed to answer.
export interface DeclaredGaps {
  open_questions: string[];
  declared: string[];
  visibility: VisibilityGap[];
}

export function declaredGaps(
  events: readonly { kind: string; payload: unknown }[],
  calls: readonly CallPayload[],
  declared: readonly string[] = [],
): DeclaredGaps {
  const record = recordOf(events);
  return {
    open_questions: [...record.questions.values()].filter((question) => question.status === "open").map((question) => question.question),
    declared: [...declared],
    visibility: calls.filter((call) => classOf(call) === "visibility_gap").map((call) => ({ ref: call.ref, tool: call.tool, failure: call.failure })),
  };
}

// The record bears on a cited ref: a supports or weakens link to a served call. What a
// guarded answer must stand on when the plugin requires a record.
export function grounded(events: readonly { kind: string; payload: unknown }[], calls: readonly CallPayload[], cited: readonly string[]): boolean {
  const served = new Set(calls.filter((call) => classOf(call) === "served").map((call) => call.ref));
  const wanted = new Set(cited);
  return recordOf(events).links.some((link) => link.relation !== "neither" && served.has(link.evidence_id) && wanted.has(link.evidence_id));
}

// --- the tools ---------------------------------------------------------------

function local(id: string, description: string, parameters: Record<string, unknown>, execute: (args: Record<string, unknown>) => Promise<ToolResult>): RegisteredTool {
  return defineTool({ id, description, parameters, execute }, { maxRows: 50, timeoutMs: 600_000 }, true);
}

export function recordTools(backend: RecordBackend): RegisteredTool[] {
  return [
    local(
      PROPOSE_TOOL,
      "Put one claim about what is happening on the record. Returns its id.",
      { type: "object", additionalProperties: false, required: ["statement"], properties: { statement: { type: "string" }, benign: BENIGN } },
      async (args) => {
        const statement = typeof args["statement"] === "string" ? args["statement"].trim() : "";
        if (statement === "") return refusedWith("a hypothesis needs a statement");
        return backend.propose(statement, benignOf(args));
      },
    ),
    local(
      LINK_TOOL,
      `Record how evidence bears on hypotheses. evidence_id is ${backend.evidence}. Name each hypothesis by hypothesis_id, or by statement to propose it here. Failed results are not evidence.`,
      {
        type: "object",
        additionalProperties: false,
        required: ["links"],
        properties: {
          links: {
            type: "array",
            items: {
              type: "object",
              additionalProperties: false,
              required: ["evidence_id", "stance"],
              properties: {
                evidence_id: { type: "string" },
                hypothesis_id: { type: "string" },
                statement: { type: "string" },
                benign: BENIGN,
                stance: { type: "string", enum: [...backend.stances] },
                note: { type: "string", description: "What this result shows, in one sentence naming the host, account or values it holds." },
              },
            },
          },
        },
      },
      async (args) => {
        const raw = Array.isArray(args["links"]) ? (args["links"] as Record<string, unknown>[]) : [];
        if (raw.length === 0) return refusedWith("name at least one link");
        const bad = raw.find((one) => !backend.stances.includes(String(one["stance"] ?? "") as Stance));
        if (bad !== undefined) return refusedWith(`stance must be one of ${backend.stances.join(", ")}`);
        const unnamed = raw.find((one) => typeof one["hypothesis_id"] !== "string" && typeof one["statement"] !== "string");
        if (unnamed !== undefined) return refusedWith("each link names a hypothesis_id, or a statement to propose one");
        // A statement is proposed once however many links name it, before any link
        // is written: one call keeps a whole step of the record.
        const proposed = new Map<string, string>();
        for (const one of raw) {
          const statement = typeof one["hypothesis_id"] === "string" ? null : String(one["statement"]).trim();
          if (statement === null || statement === "" || proposed.has(statement)) continue;
          const result = await backend.propose(statement, benignOf(one));
          const id = result.ok ? (result.rows[0] as { hypothesis_id?: unknown } | undefined)?.hypothesis_id : undefined;
          if (typeof id !== "string") return result.ok ? refusedWith(`could not propose: ${statement}`) : result;
          proposed.set(statement, id);
        }
        const links = raw.map((one) => ({
          evidence_id: String(one["evidence_id"] ?? ""),
          hypothesis_id: typeof one["hypothesis_id"] === "string" ? one["hypothesis_id"] : (proposed.get(String(one["statement"]).trim()) ?? ""),
          relation: String(one["stance"]) as Stance,
          ...(typeof one["note"] === "string" && one["note"].trim() !== "" ? { note: one["note"].trim() } : {}),
        }));
        const linked = await backend.link(links);
        return linked.ok && proposed.size > 0 ? okWith([...linked.rows, { proposed: Object.fromEntries(proposed) }]) : linked;
      },
    ),
    local(
      QUESTION_TOOL,
      "Record a question you could not settle. It is reported as a gap.",
      { type: "object", additionalProperties: false, required: ["question"], properties: { question: { type: "string" }, hypothesis_id: { type: "string" } } },
      async (args) => {
        const question = typeof args["question"] === "string" ? args["question"].trim() : "";
        if (question === "") return refusedWith("a question needs text");
        return backend.ask(question, typeof args["hypothesis_id"] === "string" ? args["hypothesis_id"] : null);
      },
    ),
  ];
}

function benignOf(args: Record<string, unknown>): boolean | undefined {
  return typeof args["benign"] === "boolean" ? args["benign"] : undefined;
}

export function okWith(rows: readonly unknown[]): ToolResult {
  return { ok: true, rows, rowCount: rows.length, capped: false, sourceSystem: "record" };
}

export function refusedWith(detail: string): ToolResult {
  return { ok: false, failure: { kind: "refused", detail } };
}

// --- the kernel's own backend --------------------------------------------------

export interface KernelRecordPorts {
  read(): Promise<readonly { kind: string; payload: unknown }[]>;
  write(kind: "hypothesis" | "link" | "question", payload: HypothesisPayload | LinkPayload | QuestionPayload): Promise<void>;
}

// Evidence is a call ref, and only one whose call was served: linking a refused
// call or a timeout as evidence would count a blind spot as an observation.
export function kernelRecord(ports: KernelRecordPorts): RecordBackend {
  return {
    stances: ["supports", "weakens"],
    evidence: "the ref a tool result opened with, such as r3",
    propose: async (statement, benign) => {
      const record = recordOf(await ports.read());
      const hypothesis_id = `h${record.hypotheses.size + 1}`;
      await ports.write("hypothesis", { hypothesis_id, statement, status: "active", provenance: "agent", resolution_reason: null, ...(benign === undefined ? {} : { benign }) });
      return okWith([{ hypothesis_id }]);
    },
    link: async (links) => {
      const events = await ports.read();
      const record = recordOf(events);
      const calls = events.filter((event) => event.kind === "call").map((event) => event.payload as CallPayload);
      const byRef = new Map(calls.map((call) => [call.ref, call]));
      for (const one of links) {
        if (!record.hypotheses.has(one.hypothesis_id)) return refusedWith(`${one.hypothesis_id} is not on the record; propose it first`);
        const call = byRef.get(one.evidence_id);
        if (call === undefined) return refusedWith(`${one.evidence_id} is not a result this run holds`);
        const kind = classOf(call);
        if (kind !== "served") return refusedWith(`${one.evidence_id} was a ${kind === "defect" ? "defective call" : "visibility gap"}, not evidence`);
      }
      for (const one of links) await ports.write("link", one);
      return okWith([{ linked: links.length }]);
    },
    ask: async (question, hypothesisId) => {
      const record = recordOf(await ports.read());
      if (hypothesisId !== null && !record.hypotheses.has(hypothesisId)) return refusedWith(`${hypothesisId} is not on the record`);
      const question_id = `q${record.questions.size + 1}`;
      await ports.write("question", { question_id, question, status: "open", hypothesis_id: hypothesisId });
      return okWith([{ question_id }]);
    },
  };
}
