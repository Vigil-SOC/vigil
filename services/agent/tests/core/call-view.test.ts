import { describe, expect, it } from "vitest";
import { callViews, queryOf, rowsIn } from "../../core/call-view.js";

// A row of the Checked tab: the call's ref, the query it sent, how many rows came
// back, how the kernel classed it, and the turn's words, read off the ledger call.

describe("a call as a reader checks it", () => {
  const [served, lost] = callViews([
    {
      iteration: 2,
      query_intent: "count the flows",
      calls: [
        {
          tool: "splunk-mcp_splunk_execute",
          ref: "r6",
          arguments: '{"spl_query":"index=botsv3 | stats count by dest_ip","earliest_time":"-24h"}',
          result: '<vigil:tool_result tool="splunk_execute" ref="r6">\n25 row(s) from splunk\n[]\n</vigil:tool_result>',
          class: "served",
          thought: "count the flows",
          duration_ms: 420,
        },
        { tool: "lookup", ref: "r7", arguments: "{}", result: "", class: "visibility_gap" },
      ],
    },
  ]);

  it("carries the ref, the query, the rows and the class", () => {
    expect(served).toMatchObject({ ref: "r6", query: "index=botsv3 | stats count by dest_ip", rows: 25, class: "served", thought: "count the flows", iteration: 2 });
  });

  it("leaves out a query it does not have and rows it was not told", () => {
    expect(lost).toMatchObject({ ref: "r7", class: "visibility_gap" });
    expect(lost).not.toHaveProperty("query");
    expect(lost).not.toHaveProperty("rows");
    expect(lost).not.toHaveProperty("thought");
  });

  it("reads the query under the names query tools give it, else the arguments whole", () => {
    expect(queryOf({ kql: "SigninLogs | take 5" })).toBe("SigninLogs | take 5");
    expect(queryOf('{"ip":"45.77.53.176"}')).toBe("45.77.53.176");
    expect(queryOf({ window: "1h" })).toBe('{"window":"1h"}');
    expect(queryOf("not json")).toBe("not json");
  });
});

describe("how many rows a call got back", () => {
  it("counts a wrapped answer's results, not the wrapper", () => {
    const wrapped = ['<vigil:tool_result tool="splunk_execute" ref="r5">', "1 row(s) from splunk", JSON.stringify([{ success: true, count: 30, results: Array.from({ length: 30 }, (_, i) => ({ i })) }]), "</vigil:tool_result>"].join("\n");
    expect(rowsIn(wrapped)).toBe(30);
  });

  it("takes the count as said when the rows are the rows", () => {
    expect(rowsIn("1 row(s) from okta\n[{\"user\":\"ada\"}]")).toBe(1);
    expect(rowsIn("25 row(s) from splunk\n[]")).toBe(25);
    expect(rowsIn("no rows here")).toBeUndefined();
  });
});
