import {
  ACCOUNT_SCHEMA_VERSION,
  CONFIDENCES,
  DISPOSITIONS,
  type CaseAccount,
  type EvidenceItem,
  type Explanation,
  type Gap,
  type Standing,
  type Step,
  type TimelineEntry,
  type Verdict,
} from "../../contracts/account.js";
import type { CallPayload, HypothesisPayload, LinkPayload } from "../../contracts/events.js";
import { agentProjection } from "../../core/agent-view.js";
import { LINK_TOOL, PROPOSE_TOOL, QUESTION_TOOL, recordOf } from "../../core/record.js";

// An investigation's account: its answer, the record it kept on the way, and its
// steps, in the shape the case page reads. Everything here is already on the ledger;
// the record (explanations, what bore on them, open questions) was journaled and
// never shown.

interface Envelope {
  kind: string;
  ts: string;
  seq: number;
  payload: unknown;
}

// A run that rules on nothing still leans: what its links say about a claim it left
// active. A ruling, when a plugin patches one in, wins.
const RULED: Record<string, Standing> = {
  proven: "proven",
  disproven: "ruled_out",
  inconclusive: "inconclusive",
  parked: "set_aside",
};

function standingOf(hypothesis: HypothesisPayload, links: readonly LinkPayload[]): Standing {
  const ruled = RULED[hypothesis.status];
  if (ruled !== undefined) return ruled;
  const on = links.filter((link) => link.hypothesis_id === hypothesis.hypothesis_id);
  const supports = on.filter((link) => link.relation === "supports").length;
  const weakens = on.filter((link) => link.relation === "weakens").length;
  return supports > weakens ? "supported" : weakens > supports ? "weakened" : "open";
}

// Older ledgers carry no role, so the claim's own opening words decide: the benign
// explanation is the one the agent introduced as benign, legitimate or authorized,
// whether it leads with the word or says "this is" one.
const SOUNDS_BENIGN = /^\s*(?:(?:this|it|the activity) is (?:an? )?)?(?:benign|legitimate|authori[sz]ed|expected|false[ -]?positive|approved|sanctioned)\b/i;

function roleOf(hypothesis: HypothesisPayload): Explanation["role"] {
  if (hypothesis.benign !== undefined) return hypothesis.benign ? "benign" : "adversary";
  return SOUNDS_BENIGN.test(hypothesis.statement) ? "benign" : "adversary";
}

// "N row(s) from <source>" opens every rendered result (core/security.ts).
const SOURCE = /^(?:<vigil:tool_result[^>]*>\s*)?\d+ rows?(?:\(s\))? from (\S+)/;

const RECORD_TOOLS = new Set([PROPOSE_TOOL, LINK_TOOL, QUESTION_TOOL]);

const text = (value: unknown): string | undefined => (typeof value === "string" && value.trim() !== "" ? value.trim() : undefined);
const texts = (value: unknown): string[] => (Array.isArray(value) ? value.map(text).filter((one): one is string => one !== undefined) : []);

// The first sentence, for a headline. A summary that opens with a shouted label
// ("CONFIRMED MALICIOUS — ...") keeps what follows the label; a hyphen only marks one
// with a space either side, so "SSH-based" and "US-EAST-1" stay whole. A sentence ends
// where the next one starts with a capital, so "e.g. procdump" does not end it.
export function headlineOf(summary: string): string {
  return opening(sentencesOf(summary), 1);
}

// A summary written as a list or in paragraphs opens with its first paragraph, its
// list markers dropped, inline ("macro. 2. It ran") too, so a marker never ends a sentence.
function sentencesOf(summary: string): string[] {
  const unlabelled = summary.replace(/^[A-Z][A-Z /]*?\s*(?:—|:|\s-\s)\s*/, "");
  const first = (unlabelled.split(/\n\s*\n/)[0] ?? unlabelled).replace(/^\s*(?:[-*]|\d+[.)])\s+/gm, "").replace(/\s*\n\s*/g, " ").replace(/(?<=[.!?:])\s+\d+[.)]\s+/g, " ");
  return (first.match(/.+?[.!?](?=\s+[A-Z("']|\s*$)|.+$/gs) ?? [first]).map((one) => one.trim());
}

function opening(sentences: readonly string[], count: number): string {
  const joined = sentences.slice(0, count).join(" ");
  return joined.charAt(0).toUpperCase() + joined.slice(1);
}

// An answer's summary is often its whole account, and the case page leads with a
// short one: its opening sentences, with the whole summary kept as what happened.
const OPENING_SENTENCES = 2;

// Each link is an observation: the result it cites, what the agent said it shows, and
// what it bears on. Its id is the link_evidence call and its place in that call, since a
// link has none of its own and one result can be cited for two different things.
// A call's links are written just before the call is, so links followed by some other
// call belong to a call a dead run never journaled; they are dropped, not pinned on
// the next link_evidence call.
function evidenceOf(runId: string, events: readonly Envelope[], calls: ReadonlyMap<string, CallPayload>): EvidenceItem[] {
  const items: EvidenceItem[] = [];
  let pending: LinkPayload[] = [];
  for (const event of events) {
    if (event.kind === "link") pending.push(event.payload as LinkPayload);
    if (event.kind !== "call") continue;
    const call = event.payload as CallPayload;
    if (call.tool !== LINK_TOOL) {
      pending = [];
      continue;
    }
    let asked: Record<string, unknown>[] = [];
    try {
      const parsed = JSON.parse(call.arguments) as { links?: unknown };
      asked = Array.isArray(parsed.links) ? (parsed.links as Record<string, unknown>[]) : [];
    } catch {
      asked = [];
    }
    pending.forEach((link, at) => {
      const cited = calls.get(link.evidence_id);
      // Before links carried a note, agents wrote what the result shows as the statement
      // beside a hypothesis_id, where it proposes nothing.
      const said = link.note ?? (typeof asked[at]?.["hypothesis_id"] === "string" ? text(asked[at]?.["statement"]) : undefined);
      const source = cited === undefined ? undefined : SOURCE.exec(cited.result)?.[1];
      items.push({
        id: `${call.ref}.${at}`,
        run_id: runId,
        claim: said ?? cited?.thought ?? `${cited?.tool ?? "result"} ${link.evidence_id}`,
        ...(source === undefined ? {} : { source }),
        ...(cited === undefined ? {} : { turn: cited.turn }),
        source_ref: link.evidence_id,
        ...(link.relation === "neither" ? {} : { bears_on: [{ on: link.hypothesis_id, stance: link.relation }] }),
      });
    });
    pending = [];
  }
  return items;
}

export function investigateAccount(runId: string, events: readonly Envelope[]): CaseAccount {
  const kernel = agentProjection(runId, events);
  const record = recordOf(events);
  const calls = new Map(kernel.calls.map((call) => [call.ref, call]));
  const value = kernel.conclusion?.value ?? {};
  const account: CaseAccount = { schema_version: ACCOUNT_SCHEMA_VERSION, runs: [{ run_id: runId, run_kind: "investigate" }] };

  const summary = text(value["summary"]);
  // The guard's final word, not the one the agent asked for: a dismissal the reviewer
  // would not uphold is held for a person, and the account says what it was held as.
  const guarded = kernel.review?.guarded;
  const disposition = guarded?.final ?? value["disposition"];
  if (summary !== undefined && (DISPOSITIONS as readonly unknown[]).includes(disposition)) {
    const confidence = value["confidence"];
    const verdict: Verdict = {
      run_id: runId,
      disposition: disposition as Verdict["disposition"],
      headline: headlineOf(summary),
      ...((CONFIDENCES as readonly unknown[]).includes(confidence) ? { confidence: confidence as Verdict["confidence"] } : {}),
      ...(guarded !== undefined && guarded.asked !== guarded.final
        ? { basis: `Held as ${guarded.final}: the agent asked for ${guarded.asked} and the reviewer did not uphold it (${kernel.review?.reasons ?? ""})` }
        : {}),
    };
    account.verdict = verdict;
  }
  if (summary !== undefined) {
    const sentences = sentencesOf(summary);
    const shown = opening(sentences, OPENING_SENTENCES);
    const longer = shown !== opening(sentences, sentences.length) || /\n/.test(summary.trim());
    account.narrative = { run_id: runId, summary: shown, ...(longer ? { what_happened: summary } : {}) };
  }

  const explanations = [...record.hypotheses.values()].map(
    (hypothesis): Explanation => ({
      id: hypothesis.hypothesis_id,
      run_id: runId,
      claim: hypothesis.statement,
      role: roleOf(hypothesis),
      standing: standingOf(hypothesis, record.links),
      ...(hypothesis.resolution_reason === null ? {} : { reason: hypothesis.resolution_reason }),
    }),
  );
  if (explanations.length > 0) account.explanations = explanations;

  const evidence = evidenceOf(runId, events, calls);
  if (evidence.length > 0) account.evidence = evidence;

  // The answer's own timeline, each entry pointed at the observation its first citation is.
  if (Array.isArray(value["timeline"])) {
    account.timeline = (value["timeline"] as Record<string, unknown>[]).flatMap((entry): TimelineEntry[] => {
      const what = text(entry["what"]);
      if (what === undefined) return [];
      const cited = texts(entry["citations"])[0];
      // Only an observation is a ref; a call cited without a link to it is not one.
      const ref = cited === undefined ? undefined : evidence.find((item) => item.source_ref === cited)?.id;
      const techniques = texts(entry["techniques"]);
      return [
        {
          run_id: runId,
          text: what,
          ...(text(entry["at"]) === undefined ? {} : { at: text(entry["at"]) as string }),
          ...(text(entry["stage"]) === undefined ? {} : { stage: text(entry["stage"]) as string }),
          ...(techniques.length === 0 ? {} : { techniques }),
          ...(ref === undefined ? {} : { ref }),
        },
      ];
    });
  }

  const next = texts(value["next_steps"]);
  if (kernel.conclusion !== null) account.next_steps = next.map((step) => ({ run_id: runId, text: step }));

  // What it could not settle: open questions and what the answer said it could not
  // settle are questions; a call the estate failed to answer is a blind spot.
  const declared = kernel.conclusion?.declared_gaps;
  if (kernel.conclusion !== null) {
    const asked = [...new Set([...(declared?.open_questions ?? []), ...texts(value["gaps"])])];
    const gaps: Gap[] = [
      ...asked.map((question): Gap => ({ run_id: runId, text: question, kind: "open_question" })),
      ...(declared?.visibility ?? []).map(
        (blind): Gap => ({ run_id: runId, text: `${blind.tool} could not answer ${blind.ref}${blind.failure === null ? "" : ` (${blind.failure})`}`, kind: "visibility" }),
      ),
    ];
    account.gaps = gaps;
  }

  // One step per turn that asked the estate something, in the agent's own words before
  // its calls, and the answer last. Keeping the record is not a step.
  const turns = new Map<number, CallPayload[]>();
  for (const call of kernel.calls) turns.set(call.turn, [...(turns.get(call.turn) ?? []), call]);
  const steps: Step[] = [...turns.entries()].flatMap(([turn, made]): Step[] => {
    const asked = made.filter((call) => !RECORD_TOOLS.has(call.tool));
    if (asked.length === 0) return [];
    const said = made.find((call) => call.thought !== undefined)?.thought ?? asked.map((call) => call.tool).join(", ");
    return [{ run_id: runId, n: turn, kind: "EXAMINE", text: said, refs: asked.map((call) => call.ref) }];
  });
  if (kernel.conclusion !== null && summary !== undefined) {
    steps.push({ run_id: runId, n: kernel.conclusion.turns + 1, kind: "CONCLUDE", text: headlineOf(summary), refs: texts(value["citations"]) });
  }
  if (steps.length > 0) account.steps = steps;

  return account;
}
