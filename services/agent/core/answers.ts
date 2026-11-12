import type { NewEvent, ResolutionPayload, RunKind } from "../contracts/events.js";
import type { State } from "./seams.js";
import { bridgeHeaders, RunRefused } from "./principal.js";

// How a human's answer reaches a run. Reading only: the answer is recorded on
// the other side and journaled here, so the ledger keeps its single writer.
export type Answers = (runId: string) => Promise<ResolutionPayload[]>;

export interface AnswersOptions {
  url: string;
  token: string;
  // The run's signed principal, sent beside the bearer.
  principal?: string;
  fetch?: typeof globalThis.fetch;
}

// Nobody to ask, so nothing was answered. A run with no answer source parks and
// stays parked, which is the honest outcome rather than proceeding unapproved.
export const noAnswers: Answers = async () => [];

const ANSWERED = new Set(["approve", "reject"]);

export function httpAnswers(options: AnswersOptions): Answers {
  const call = options.fetch ?? globalThis.fetch;
  const base = options.url.replace(/\/$/, "");

  // Unlike a progress update this throws: proceeding without an answer that
  // exists would run a call a human rejected.
  return async (runId) => {
    const response = await call(`${base}/${encodeURIComponent(runId)}/decisions`, {
      headers: bridgeHeaders(options.token, options.principal),
    });
    if (response.status === 403) throw new RunRefused();
    if (!response.ok) throw new Error(`could not read decisions for ${runId}: the endpoint answered ${response.status}`);
    return resolutionsOf(await response.json());
  };
}

export function resolutionsOf(body: unknown): ResolutionPayload[] {
  const decisions = (body as Record<string, unknown> | null)?.["decisions"];
  if (!Array.isArray(decisions)) return [];
  return decisions.filter((entry): entry is ResolutionPayload => {
    const value = entry as Record<string, unknown>;
    return typeof value?.["checkpoint_id"] === "string" && ANSWERED.has(String(value["answer"]));
  });
}

// Journals what has been answered since the run parked, skipping any the ledger
// already holds so a resume that answers nothing new appends nothing.
export async function journalAnswers<K extends Record<string, unknown>>(
  state: State<K>,
  runId: string,
  runKind: RunKind,
  answers: Answers,
): Promise<number> {
  const events = await state.read(runId);
  const held = new Set(
    events.filter((event) => event.kind === "resolution").map((event) => (event.payload as ResolutionPayload).checkpoint_id),
  );
  const raised = new Set(
    events.filter((event) => event.kind === "checkpoint").map((event) => (event.payload as { checkpoint_id: string }).checkpoint_id),
  );

  // Only against a checkpoint this run actually raised: an answer to a
  // checkpoint nobody asked for resolves nothing and would sit on the ledger.
  const fresh = (await answers(runId)).filter((one) => raised.has(one.checkpoint_id) && !held.has(one.checkpoint_id));
  if (fresh.length === 0) return 0;

  await state.append(
    runId,
    fresh.map((payload) => ({ run_id: runId, run_kind: runKind, kind: "resolution", payload }) as NewEvent<K>),
  );
  return fresh.length;
}
