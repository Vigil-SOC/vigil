import type { Message } from "../../core/provider.js";
import type { Shown } from "./tools.js";
import type { NotebookState } from "./findings.js";
import type { Survey } from "./survey.js";

// The checkpoint an auto-started trace waits at before it spends a model call. Its
// own class, not the hunt's hypothesis_approval: what an operator permits here is
// tracing one confirmed finding, and there are no hypotheses to count.
export const RCA_PERMIT = "rca_permit";

// What the investigator emits at the end of every stretch: carry on, saying what it
// learned and what it checks next, or finish with its report. The moves are the run's
// reasoning trail, the way a hunt's lead decisions are. Only FINISH halts, and only
// once the finish gate takes the report.
export const RCA_ACTIONS = ["CONTINUE", "FINISH"] as const;
export const RCA_HALTS = ["FINISH"] as const;

export interface Move {
  action: (typeof RCA_ACTIONS)[number];
  // What the last stretch established and why it matters for the trace.
  rationale: string;
  // CONTINUE: the question the next stretch goes after.
  next?: string;
  // FINISH: the report, markdown.
  report?: string;
  // FINISH: what a responder should do, most urgent first.
  next_steps?: string[];
}

// One stretch of the investigation and the move that ended it. A FINISH the gate
// sent back carries why; the transcript is what the next stretch continues from.
export interface SegmentPayload {
  segment: number;
  transcript: Message[];
  move: Move;
  // FINISH only: the report as drafted, and the gate's refusal, null when it was taken.
  draft: string | null;
  refused: string | null;
}

export type RcaKinds = {
  survey: Survey;
  query: Shown;
  notebook: NotebookState;
  segment: SegmentPayload;
};
