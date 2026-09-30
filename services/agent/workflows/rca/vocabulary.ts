import type { Message } from "../../core/provider.js";
import type { Shown } from "./tools.js";
import type { NotebookState } from "./findings.js";
import type { Survey } from "./survey.js";

// The checkpoint an auto-started trace waits at before it spends a model call. Its
// own class, not the hunt's hypothesis_approval: what an operator permits here is
// tracing one confirmed finding, and there are no hypotheses to count.
export const RCA_PERMIT = "rca_permit";

// The one verb the investigator emits. The arch's schema is checked against it.
export const RCA_ACTIONS = ["FINISH"] as const;

export interface ReportEmission {
  action: "FINISH";
  report: string;
}

// One stretch of the investigation, ended by a report draft the finish gate either
// accepted or sent back. The transcript is what the next stretch continues from.
export interface SegmentPayload {
  segment: number;
  transcript: Message[];
  draft: string | null;
  refused: string | null;
}

export type RcaKinds = {
  survey: Survey;
  query: Shown;
  notebook: NotebookState;
  segment: SegmentPayload;
};
