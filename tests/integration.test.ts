import { existsSync, mkdtempSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeEach, describe, expect, it } from "vitest";
import type OpenAI from "openai";
import { Limiter } from "../ai/limiter.js";
import { LlmDecisionProvider, LlmWorkerDispatcher, resetEmitMode } from "../ai/llm.js";
import { HuntController, startHunt } from "../ai/loop.js";
import { buildSpec } from "../ai/spec.js";
import { buildTools, closeTools, type Tool } from "../ai/tools.js";
import { Ledger, snapshots } from "../ai/ledger.js";
import { replay } from "../ai/replay.js";

type Body = OpenAI.Chat.ChatCompletionCreateParamsNonStreaming;

const DATABASE = `${homedir()}/Downloads/botsv3_duckdb/botsv3.duckdb`;

const BEACON_SQL = `WITH d AS (
  SELECT src_ip, dest_ip, dest_port,
         epoch(ts - lag(ts) OVER (PARTITION BY src_ip, dest_ip, dest_port ORDER BY ts)) AS gap
  FROM net_flow
  WHERE src_ip LIKE '192.168.%' AND dest_ip NOT LIKE '192.168.%' AND dest_ip NOT LIKE '172.16.%'
)
SELECT src_ip, dest_ip, dest_port, count(*) beacons, round(stddev(gap), 2) jitter
FROM d WHERE gap BETWEEN 1 AND 3600
GROUP BY 1, 2, 3 HAVING count(*) > 20 AND stddev(gap) < 5 ORDER BY beacons DESC`;

function completion(message: Record<string, unknown>): OpenAI.Chat.ChatCompletion {
  return {
    choices: [{ message, finish_reason: "stop", index: 0, logprobs: null }],
    usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
  } as unknown as OpenAI.Chat.ChatCompletion;
}

// Everything downstream of the HTTP call is real: the controller, both roles,
// the tool loop, the DuckDB tool, and the ledger. Only the gateway is stubbed.
function fakeGateway(hypothesisId: string, bodies: Body[]) {
  // evidence_citations is required of every emission, so the stub sends it too:
  // a fake gateway that skips it is not exercising the schema a model answers.
  const leadDecisions = [
    {
      action: "INVESTIGATE",
      rationale: "establish a beaconing baseline",
      query_intent: "find regular outbound intervals",
      evidence_citations: [],
    },
    { action: "CONCLUDE", rationale: "beaconing confirmed", evidence_citations: [] },
  ];
  let workerQueried = false;

  return async (body: Body): Promise<OpenAI.Chat.ChatCompletion> => {
    bodies.push(body);
    const isEmit = body.response_format !== undefined || body.tool_choice !== undefined;
    const isLead = String(body.messages[0]?.content ?? "").includes("Hunt Lead");

    if (isLead) {
      if (!isEmit) return completion({ role: "assistant", content: "considering the ledger" });
      return completion({ role: "assistant", content: JSON.stringify(leadDecisions.shift()) });
    }

    if (isEmit) {
      return completion({
        role: "assistant",
        content: JSON.stringify({
          results: [
            {
              source_system: "net_flow",
              summary: "192.168.70.186 beacons to 45.77.53.176:443 with 4.4s jitter over 2641 connections",
              salience: "anomalous",
              why_notable: "fixed-interval outbound to a host with no business relationship",
              payload: { src_ip: "192.168.70.186", dest_ip: "45.77.53.176", beacons: 2641, jitter: 4.4 },
              supports: [hypothesisId],
            },
          ],
          ips_to_check: ["45.77.53.176"],
        }),
      });
    }

    if (!workerQueried) {
      workerQueried = true;
      return completion({
        role: "assistant",
        tool_calls: [
          { id: "c1", type: "function", function: { name: "duckdb_query", arguments: JSON.stringify({ sql: BEACON_SQL }) } },
        ],
      });
    }
    return completion({ role: "assistant", content: "query complete" });
  };
}

describe.skipIf(!existsSync(DATABASE))("hunt end to end (stubbed gateway, real everything else)", () => {
  let tools: Tool[] = [];
  beforeEach(() => resetEmitMode());
  afterAll(async () => closeTools(tools));

  it("runs real SQL through a worker into the ledger and leaves it replayable", async () => {
    const spec = buildSpec({ workflowPath: "frothly.yaml" });
    const ledger = startHunt(spec, mkdtempSync(join(tmpdir(), "hunt-")));
    const hypothesisId = [...ledger.projection.hypotheses.keys()][0]!;

    const bodies: Body[] = [];
    const client = { chat: { completions: { create: fakeGateway(hypothesisId, bodies) } } } as unknown as OpenAI;
    const limiter = new Limiter({ rpm: 10_000, tpm: 10_000_000 }, 4, 1);
    tools = await buildTools(spec, ledger);

    const controller = new HuntController(
      ledger,
      new LlmDecisionProvider(spec, tools, limiter, client),
      new LlmWorkerDispatcher(spec, tools, limiter, client),
      spec.dispatch,
      spec.digest,
    );

    const first = await controller.advanceIteration();
    expect(first.action).toBe("INVESTIGATE");
    expect(first.evidence_appended).toBe(1);
    expect(first.cost_usd).toBeGreaterThan(0);

    // The worker's SQL really ran: the C2 came back through the tool loop.
    const toolResults = bodies.flatMap((body) => body.messages.filter((message) => message.role === "tool"));
    expect(JSON.stringify(toolResults)).toContain("45.77.53.176");

    const evidence = [...ledger.projection.evidence.values()];
    expect(evidence[0]!.salience).toBe("anomalous");
    // A declared domain, not the worker's name, and the rows behind the claim are
    // on the record — the critic and expand both read this, not the summary.
    expect(evidence[0]!.source_system).toBe("net_flow");
    expect(evidence[0]!.payload["dest_ip"]).toBe("45.77.53.176");

    // The worker's spend and the SQL it ran are on its dispatch row, so the
    // budget counter sees the largest share of a real hunt's cost.
    const dispatch = [...ledger.projection.dispatches.values()][0]!;
    expect(dispatch.cost_usd).toBeGreaterThan(0);
    expect(dispatch.calls[0]!.tool).toBe("duckdb_query");
    expect(ledger.projection.hunt.cost_usd).toBeCloseTo(first.cost_usd, 10);
    expect(first.cost_usd).toBeGreaterThan(ledger.projection.decisions[0]!.cost_usd);
    expect(ledger.projection.links).toEqual([
      { evidence_id: evidence[0]!.evidence_id, hypothesis_id: hypothesisId, relation: "supports" },
    ]);
    expect([...ledger.projection.questions.values()][0]!.question).toBe("check 45.77.53.176");

    // CONCLUDE is a recommendation, not a verdict: nothing has been validated
    // and both hypotheses are still active, so the controller refuses it.
    const second = await controller.advanceIteration();
    expect(second.hunt_status).toBe("active");
    const statuses = [...ledger.projection.hypotheses.values()].map((h) => h.status);
    expect(statuses).toEqual(["active", "active"]);
    expect(second.note).toContain("still active");

    // The digest the lead saw is on the record, and the file replays to the same state.
    const decisions = ledger.projection.decisions;
    expect(decisions).toHaveLength(2);
    expect(snapshots(ledger.log)[1]!.recent_evidence[0]!.summary).toContain("45.77.53.176");
    expect(Ledger.open(ledger.path).projection).toEqual(ledger.projection);

    // And every digest rebuilds from the prefix behind it — the determinism the
    // ledger claims, checked rather than asserted.
    const replayed = replay(ledger.log);
    expect(replayed.reproduced).toBe(decisions.length);
    expect(replayed.inexact).toBe(0);
  });
});
