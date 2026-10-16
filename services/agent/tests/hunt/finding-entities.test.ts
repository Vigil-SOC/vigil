// #2277: a hunt over findings that name a host can rule on that host. The page is the one
// the backend's search_findings returns for the six-alert chain, held to it by a Python ratchet.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { pluginGrants } from "../../core/agent.js";
import { archFor } from "../../plugins/kinds.js";
import { pluginFor } from "../../plugins/registry.js";
import type { ToolResult } from "../../contracts/tool.js";
import type { ToolDispatch } from "../../core/seams.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { citedFindings } from "../../plugins/hunt/findings.js";
import { fold, type HuntEvent, type HuntKinds } from "../../plugins/hunt/ledger.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { FIXTURES, RUN, harnessOf, hypothesisIn, modelOf, shown, specOf, type Step } from "../support/kernel-hunt.js";

interface Row {
  finding_id: string;
  data_source: string;
  summary: string;
  entities?: { hosts?: string[]; users?: string[] };
}

const CHAIN = JSON.parse(readFileSync(join(FIXTURES, "findings", "dusky-lantern-chain.json"), "utf8")) as {
  alerts: { id: string; raw_data: Record<string, unknown> }[];
  page: { findings: Row[] };
};
const CLAIM = "DUSKY LANTERN ran its full chain on one finance workstation";

// The backend: a search answers the page as one row, get_finding one alert with its raw record.
function backend(page: unknown): ToolDispatch {
  return {
    invoke: async (tool, args): Promise<ToolResult> => {
      if (tool.local) return tool.invoke(args);
      if (tool.id === "get_finding") {
        const alert = CHAIN.alerts.find((one) => one.id === args["finding_id"]);
        const row = { finding_id: alert?.id, entity_context: { source_evidence: { records: [alert?.raw_data] } } };
        return { ok: true, rows: [row], rowCount: 1, capped: false, sourceSystem: "vigil" };
      }
      return { ok: true, rows: [page], rowCount: 1, capped: false, sourceSystem: "vigil" };
    },
  };
}

// The rows of the latest result from one tool, as the model was shown them.
function latest(request: TurnRequestLike, tool: string): unknown[] {
  const text = shown(request as never);
  const opened = text.lastIndexOf(`<vigil:tool_result tool="${tool}"`);
  const body = text.slice(text.indexOf("\n", opened) + 1, text.indexOf("</vigil:tool_result>", opened));
  return JSON.parse(body.slice(body.indexOf("\n") + 1)) as unknown[];
}
type TurnRequestLike = Parameters<Step>[0];

// What a careful hunter does with a page: group the alerts by the host each names, and rule on
// the host that carries the chain. A row naming no host is all it can say "unknown" about.
const byHost: Step = (request) => {
  const [page] = latest(request, "search_findings") as { findings: Row[] }[];
  const hosts = new Map<string, Row[]>();
  for (const row of page!.findings) {
    const host = row.entities?.hosts?.[0] ?? "host not named in the finding summary";
    hosts.set(host, [...(hosts.get(host) ?? []), row]);
  }
  const [host, rows] = [...hosts.entries()].sort((a, b) => b[1].length - a[1].length)[0]!;
  return {
    tool: "record_evidence",
    args: {
      results: [
        {
          source_system: "endpoint",
          summary: `${rows.length} alerts on ${host}: ${rows.map((row) => row.finding_id).join(", ")}`,
          salience: "anomalous",
          why_notable: "one host carries delivery, execution, C2, credential access and lateral movement",
          payload: JSON.stringify({ rows: rows.map((row) => ({ finding_id: row.finding_id, host, data_source: row.data_source })) }),
          supports: [hypothesisIn(request)],
          rests_on: [{ field: "host", authored: "sensor" }],
        },
      ],
    },
  };
};

const steps: Step[] = [
  () => ({ tool: "search_findings", args: { query: "" } }),
  byHost,
  () => ({ tool: "get_finding", args: { finding_id: "dl-003" } }),
  (request) => {
    const [row] = latest(request, "get_finding") as { finding_id: string; entity_context: { source_evidence: { records: Record<string, string>[] } } }[];
    const record = row!.entity_context.source_evidence.records[0]!;
    return {
      tool: "record_evidence",
      args: {
        results: [
          {
            source_system: "process_lineage",
            summary: `${record["command_line"]} on ${record["host"]} as ${record["user"]}`,
            salience: "anomalous",
            why_notable: "an unsigned DLL from Temp, under the user the ISO was delivered to",
            payload: JSON.stringify({ rows: [{ finding_id: row!.finding_id, ...record }] }),
            supports: [hypothesisIn(request)],
            rests_on: [{ field: "parent_process", authored: "sensor" }],
          },
        ],
      },
    };
  },
  () => ({ say: "FIN-WS-114 carries the chain." }),
];

async function hunt(page: unknown, linked: string[] = []) {
  const state = new InProcessState<HuntKinds>();
  const spec = specOf("hunt", [CLAIM]);
  await runHunt(harnessOf(spec, modelOf(steps), state, backend(page)), {
    run_id: RUN,
    run_kind: "hunt",
    spec,
    actions: archFor("hunt").actions,
    queue: new InProcessDirectiveQueue(),
    onFindings: async (_run, ids) => {
      linked.push(...ids);
      return true;
    },
  });
  return fold((await state.read(RUN)) as readonly HuntEvent[]);
}

describe("a hunt over the six-alert chain on FIN-WS-114", () => {
  it("reads each finding's host off the page and records evidence naming FIN-WS-114", async () => {
    const linked: string[] = [];
    const projection = await hunt(CHAIN.page, linked);
    const observed = [...projection.evidence.values()].filter((record) => record.provenance === "worker");

    expect(observed).toHaveLength(2);
    for (const record of observed) {
      expect(record.summary).toContain("FIN-WS-114");
      expect(record.summary).not.toMatch(/not named|unknown/);
    }
    // Five alerts name the host; the ISO email names only priya.nair, and the raw record
    // opened with get_finding names her too.
    expect(observed[0]!.summary).toMatch(/^5 alerts on FIN-WS-114: dl-002, dl-003, dl-004, dl-005, dl-006$/);
    expect(observed[1]!.summary).toContain("priya.nair");
    // And the chain is what reaches the case.
    expect(citedFindings(projection).sort()).toEqual(["dl-002", "dl-003", "dl-004", "dl-005", "dl-006"]);
    expect([...new Set(linked)].sort()).toEqual(["dl-002", "dl-003", "dl-004", "dl-005", "dl-006"]);
  });

  it("is granted get_finding, so the raw record behind an alert is a call away", () => {
    const plugin = pluginFor("hunt");
    expect(plugin.needs).toContain("get_finding");
    expect(Object.values(pluginGrants(plugin, specOf("hunt", [CLAIM]).tools)).flat()).toContain("get_finding");
  });

  // The page as it was before #2277: no entities, so the host is all it can call unknown.
  it("could only rule the host unknown on a page that carried no entities", async () => {
    const bare = { ...CHAIN.page, findings: CHAIN.page.findings.map(({ entities: _entities, ...row }) => row) };
    const projection = await hunt(bare);
    const [first] = [...projection.evidence.values()].filter((record) => record.provenance === "worker");

    expect(first!.summary).toContain("host not named in the finding summary");
  });
});
