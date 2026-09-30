import { describe, expect, it } from "vitest";
import { bounded, isSplunk, writes } from "../../workflows/rca/siem.js";

describe("the search a root-cause run sends", () => {
  // splunk_execute takes no latest, and an ISO time in the search matches nothing.
  it("writes both bounds onto the base search as epoch time modifiers", () => {
    expect(bounded('index=main sourcetype=net dest="a|b" | stats count by host', { earliest: 100.9, latest: 200 })).toBe(
      'index=main sourcetype=net dest="a|b" earliest=100 latest=200 | stats count by host',
    );
    expect(bounded("index=main pid=4242", { latest: 50 })).toBe("index=main pid=4242 latest=50");
  });

  it("leaves a generating search and an unbounded one as written", () => {
    expect(bounded("| tstats count where index=* by sourcetype", { earliest: 1 })).toBe("| tstats count where index=* by sourcetype");
    expect(bounded("index=main", {})).toBe("index=main");
  });

  it("refuses write commands and knows splunk_execute under any server name", () => {
    expect(writes("index=main | outputlookup x.csv")).toBe(true);
    expect(writes("index=main | stats count")).toBe(false);
    expect(isSplunk("splunk-selfhosted_splunk_execute")).toBe(true);
    expect(isSplunk("splunk_run_splunk_search")).toBe(false);
  });
});
