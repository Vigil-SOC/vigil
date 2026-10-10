import { defineTool, type RegisteredTool, type ToolResult } from "../contracts/tool.js";
import type {
  CallClass,
  CallPayload,
  HypothesisPayload,
  LinkPayload,
  PatchPayload,
  QuestionPayload,
} from "../contracts/events.js";
import { isVisibilityGap } from "./security.js";

// The hypothesis record: what a run believes, what bears on each belief, and what
// it could not answer. Kept through three kernel tools so every plugin keeps it the
// same way, and folded by one reader so every plugin's is read the same way. A
// plugin whose record is richer supplies its own backend; the events
// it writes carry every field the kernel's do, so this fold reads them too.

export const PROPOSE_TOOL = "propose_hypothesis";
export const LINK_TOOL = "link_evidence";
export const QUESTION_TOOL = "open_question";
export const RECORD_TOOLS = [PROPOSE_TOOL, LINK_TOOL, QUESTION_TOOL] as const;

export type Stance = LinkPayload["relation"];

export interface ProposedLink {
  evidence_id: string;
  hypothesis_id: string;
  relation: Stance;
}

// Where the three tools write. Each answers as a tool does, so a refusal reaches
// the model as one and the call is journaled as a defect.
export interface RecordBackend {
  // What link_evidence accepts. The kernel's own takes supports and weakens.
  stances: readonly Stance[];
  // What link_evidence's evidence_id names, for the tool's description.
  evidence: string;
  propose(statement: string): Promise<ToolResult>;
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

export interface VisibilityGap {
  ref: string;
  tool: string;
  failure: string | null;
}

// What the run could not answer, declared rather than discovered by a reader:
// questions it opened and never closed, what it said it could not settle, and
// every call the estate failed to answer.
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

// The record bears on a cited ref: some hypothesis has a supports or weakens link
// to a call that was served. What a guarded answer must stand on when the plugin
// requires a record.
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
      "Put a hypothesis on the record: one claim about what is happening that evidence could support or weaken. Returns its id. Propose the benign explanation too.",
      { type: "object", additionalProperties: false, required: ["statement"], properties: { statement: { type: "string" } } },
      async (args) => {
        const statement = typeof args["statement"] === "string" ? args["statement"].trim() : "";
        if (statement === "") return refusedWith("a hypothesis needs a statement");
        return backend.propose(statement);
      },
    ),
    local(
      LINK_TOOL,
      `Record how evidence bears on hypotheses. evidence_id is ${backend.evidence}. A result that failed or was refused is not evidence and is refused here.`,
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
              required: ["evidence_id", "hypothesis_id", "stance"],
              properties: {
                evidence_id: { type: "string" },
                hypothesis_id: { type: "string" },
                stance: { type: "string", enum: [...backend.stances] },
              },
            },
          },
        },
      },
      async (args) => {
        const raw = Array.isArray(args["links"]) ? (args["links"] as Record<string, unknown>[]) : [];
        const links = raw.map((one) => ({
          evidence_id: String(one["evidence_id"] ?? ""),
          hypothesis_id: String(one["hypothesis_id"] ?? ""),
          relation: String(one["stance"] ?? "") as Stance,
        }));
        const bad = links.find((one) => !backend.stances.includes(one.relation));
        if (bad !== undefined) return refusedWith(`stance must be one of ${backend.stances.join(", ")}`);
        if (links.length === 0) return refusedWith("name at least one link");
        return backend.link(links);
      },
    ),
    local(
      QUESTION_TOOL,
      "Record a question the run has not answered: a lead worth following, or something it could not settle. Open questions are declared gaps in the conclusion.",
      { type: "object", additionalProperties: false, required: ["question"], properties: { question: { type: "string" }, hypothesis_id: { type: "string" } } },
      async (args) => {
        const question = typeof args["question"] === "string" ? args["question"].trim() : "";
        if (question === "") return refusedWith("a question needs text");
        return backend.ask(question, typeof args["hypothesis_id"] === "string" ? args["hypothesis_id"] : null);
      },
    ),
  ];
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
    propose: async (statement) => {
      const record = recordOf(await ports.read());
      const hypothesis_id = `h${record.hypotheses.size + 1}`;
      await ports.write("hypothesis", { hypothesis_id, statement, status: "active", provenance: "agent", resolution_reason: null });
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
