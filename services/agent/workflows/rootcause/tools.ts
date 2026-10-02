import type { BudgetLimits } from "../../contracts/budget.js";
import type { ToolResult } from "../../contracts/tool.js";
import type { LocalExecutor } from "../../tools/local.js";
import type { State } from "../../core/seams.js";
import {
  ceilingsHit,
  isProven,
  latestSteps,
  observationsOf,
  openSteps,
  originOf,
  proveLink,
  type RootCauseKinds,
  type StepPayload,
} from "./proof.js";

function text(args: Record<string, unknown>, key: string): string {
  const value = args[key];
  return typeof value === "string" ? value.trim() : "";
}

function failure(detail: string): ToolResult {
  return { ok: false, failure: { kind: "refused", detail } };
}

function ok(row: Record<string, unknown>): ToolResult {
  return { ok: true, rows: [row], rowCount: 1, capped: false, sourceSystem: "ledger" };
}

export function recordFrom(state: State<RootCauseKinds>, runId: string): LocalExecutor {
  return async (args): Promise<ToolResult> => {
    const event = text(args, "event");
    const at = text(args, "at");
    if (event === "" || at === "") return failure("record needs event and at");

    const events = await state.read(runId);
    const steps = latestSteps(events);
    const asked = text(args, "step_id");
    const existing = asked === "" ? undefined : steps.find((step) => step.step_id === asked);
    if (asked !== "" && existing === undefined) return failure(`no step ${asked} is on this run`);

    const causeId = text(args, "cause_id");
    const cause = causeId === "" ? undefined : steps.find((step) => step.step_id === causeId);
    if (causeId !== "" && cause === undefined) return failure(`no cause step ${causeId} is on this run`);

    const who = text(args, "who");
    const link = text(args, "link");
    const artifact = text(args, "artifact");
    const origin = args["origin"] === true;
    const whos = [who, cause?.who ?? ""];
    const observations = observationsOf(events);
    const step: StepPayload = {
      step_id: existing?.step_id ?? `step-${steps.length + 1}`,
      event,
      who,
      at,
      link,
      artifact,
      cause_id: causeId === "" ? null : causeId,
      origin,
      link_status: causeId === "" ? "none" : proveLink(link, whos, cause?.at ?? "", observations),
      origin_status: origin ? originOf(link, artifact, whos, at, observations) : "none",
    };

    await state.append(runId, [{ run_id: runId, run_kind: "root_cause", kind: "step", payload: step }]);
    return ok({
      step_id: step.step_id,
      link_status: step.link_status,
      origin_status: step.origin_status,
      proven: isProven(step),
    });
  };
}

export function finishFrom(
  state: State<RootCauseKinds>,
  runId: string,
  limits: BudgetLimits,
  now: () => number = Date.now,
): LocalExecutor {
  return async (): Promise<ToolResult> => {
    const events = await state.read(runId);
    const steps = latestSteps(events);
    const open = openSteps(steps);
    if ((steps.length === 0 || open.length > 0) && !ceilingsHit(events, limits, now())) {
      const which = open.length === 0 ? "nothing has been recorded" : open.map((step) => step.step_id).join(", ");
      return failure(`finish refused; a cause is still open (${which})`);
    }
    return ok({ finished: true, open: open.map((step) => step.step_id) });
  };
}
