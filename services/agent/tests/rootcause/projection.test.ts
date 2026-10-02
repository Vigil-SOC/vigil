import { describe, expect, it } from "vitest";
import type { NewEvent } from "../../contracts/events.js";
import { InProcessState } from "../../core/state.js";
import { rootCauseProjection, SEARCHES_SHOWN } from "../../workflows/rootcause/projection.js";
import type { RootCauseKinds, StepPayload } from "../../workflows/rootcause/proof.js";

const RUN = "5a2c2d3e-0000-4000-8000-000000000c39";
const AT = "2024-01-02T03:00:00Z";

type Draft = Omit<NewEvent<RootCauseKinds>, "run_id" | "run_kind">;

async function ledger(...drafts: Draft[]) {
  const state = new InProcessState<RootCauseKinds>();
  await state.append(
    RUN,
    drafts.map((draft) => ({ ...draft, run_id: RUN, run_kind: "root_cause" }) as NewEvent<RootCauseKinds>),
  );
  return state.read(RUN);
}

const step = (over: Partial<StepPayload>): Draft => ({
  kind: "step",
  payload: {
    step_id: "s1",
    event: "file landed",
    who: "alice",
    at: AT,
    link: "invoice.lnk",
    artifact: "",
    cause_id: null,
    origin: false,
    link_status: "unproven",
    origin_status: "none",
    ...over,
  },
});

const spend = (cost_usd: number | null): Draft =>
  ({ kind: "spend", payload: { model_id: "m", provider_type: "p", role: "lead", cost_usd } }) as unknown as Draft;

const search = (tool: string, args: string, rows = 3): Draft =>
  ({
    kind: "dispatch",
    payload: {
      dispatch_id: `dsp-${args}`,
      agent_id: "lead",
      status: "complete",
      question_id: null,
      failure_reason: null,
      result: { ok: true, rows: [{ secret: "row" }], rowCount: rows, capped: false, sourceSystem: "x" },
      calls: [{ tool, arguments: args }],
    },
  }) as unknown as Draft;

const permit = { checkpoint_id: "cp-root-cause-permit", checkpoint_class: "hypothesis_approval", question: "Permit?", raised_at: AT };

describe("the root-cause projection", () => {
  it("shows a run in flight: open steps, summed spend, searches without rows, no terminal", async () => {
    const failed = search("splunk_execute", "bad").payload as { status: string; failure_reason: string; result: unknown };
    failed.status = "failed";
    failed.failure_reason = "timeout";
    failed.result = { ok: false, failure: { kind: "timeout" } };

    const view = rootCauseProjection(
      RUN,
      await ledger(
        spend(0.5),
        step({}),
        step({ step_id: "s2", link_status: "proven", cause_id: "s1" }),
        step({ step_id: "s1", origin: true, origin_status: "proven", link_status: "proven" }),
        search("splunk_execute", "index=main", 7),
        { kind: "dispatch", payload: { dispatch_id: "g", agent_id: "lead", status: "complete", question_id: null, failure_reason: null } },
        { kind: "dispatch", payload: failed } as unknown as Draft,
        spend(null),
        spend(0.25),
        { kind: "notice", payload: { text: "unprovable here" } },
      ),
    );

    expect(view).toMatchObject({ kind: "root_cause", status: "running", outcome: null, reason: "", cost_usd: 0.75, open_checkpoint: null });
    expect(view.steps.map((s) => [s.step_id, s.proven])).toEqual([["s1", true], ["s2", true]]);
    expect(view.searches).toEqual([
      { tool: "splunk_execute", arguments: "index=main", row_count: 7, failure: null },
      { tool: "splunk_execute", arguments: "bad", row_count: null, failure: "timeout" },
    ]);
    expect(JSON.stringify(view)).not.toContain("secret");
    expect(view.notices).toEqual(["unprovable here"]);
  });

  it("leaves an unpriced run's cost null and keeps an open step open", async () => {
    const view = rootCauseProjection(RUN, await ledger(spend(null), step({})));
    expect(view.cost_usd).toBeNull();
    expect(view.steps[0]).toMatchObject({ link_status: "unproven", proven: false });
  });

  it("reports a run parked at the permit as waiting, with the checkpoint", async () => {
    const view = rootCauseProjection(RUN, await ledger({ kind: "checkpoint", payload: permit }));
    expect(view.status).toBe("waiting_approval");
    expect(view.open_checkpoint?.checkpoint_id).toBe("cp-root-cause-permit");

    const resumed = await ledger(
      { kind: "checkpoint", payload: permit },
      { kind: "resolution", payload: { checkpoint_id: permit.checkpoint_id, actor: "a", answer: "approve", text: "", resolved_at: AT } },
    );
    expect(rootCauseProjection(RUN, resumed)).toMatchObject({ status: "running", open_checkpoint: null });
  });

  it("reports the terminal outcome and reason", async () => {
    const view = rootCauseProjection(
      RUN,
      await ledger(step({}), { kind: "terminal", payload: { outcome: "completed", reason: "the trace finished", summary: "report" } }),
    );
    expect(view).toMatchObject({ status: "terminal", outcome: "completed", reason: "the trace finished" });
    expect(JSON.stringify(view)).not.toContain("report");
  });

  it("caps the searches and keeps the true total", async () => {
    const many = Array.from({ length: SEARCHES_SHOWN + 5 }, (_, at) => search("t", `q${at}`));
    const view = rootCauseProjection(RUN, await ledger(...many));
    expect(view.searches).toHaveLength(SEARCHES_SHOWN);
    expect(view.search_count).toBe(SEARCHES_SHOWN + 5);
    expect(view.searches.at(-1)?.arguments).toBe(`q${SEARCHES_SHOWN + 4}`);
  });

  it("folds an empty ledger", () => {
    expect(rootCauseProjection(RUN, [])).toMatchObject({
      status: "running",
      cost_usd: null,
      steps: [],
      searches: [],
      search_count: 0,
      notices: [],
    });
  });
});
