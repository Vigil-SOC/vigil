import { describe, expect, it } from "vitest";
import { PROVER_TOOL, proveLink, proveOrigin, redact, type Observation, type StepPayload } from "../../plugins/rootcause/proof.js";

const AT = "2024-01-02T03:00:00Z";
const LINK = "invoice.lnk";

function observation(tool: string, args: string, rows: readonly unknown[]): Observation {
  return { tool, args, rows };
}

function count(tool: string, value: string, at: string, n: number): Observation {
  return observation(tool, `search ${value} before ${at}`, [{ count: n }]);
}

function hit(value: string): Observation {
  return observation(PROVER_TOOL, "", [{ file: value }]);
}

const BOTH = [hit(LINK), hit(LINK)];
const ZERO = count(PROVER_TOOL, LINK, AT, 0);

describe("a link is a value both events carry", () => {
  it("is proved only by two results and a zero count before the cause", () => {
    expect(proveLink(LINK, [], AT, [...BOTH, ZERO])).toBe("proven");
  });

  it("rejects an IP, a timestamp, or the step's who", () => {
    expect(proveLink("203.0.113.5", [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink("2001:db8::1", [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink(AT, [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink("10:01:39", [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink("alice", ["alice"], AT, [...BOTH, ZERO])).toBe("rejected");
  });

  it("rejects a value counted before the cause and leaves a missing count unproven", () => {
    expect(proveLink(LINK, [], AT, [...BOTH, count(PROVER_TOOL, LINK, AT, 4)])).toBe("rejected");
    expect(proveLink(LINK, [], AT, BOTH)).toBe("unproven");
  });

  it("treats an empty hit list as nothing", () => {
    const empty = observation(PROVER_TOOL, `search ${LINK} before ${AT}`, []);
    expect(proveLink(LINK, [], AT, [empty, empty, ZERO])).toBe("unproven");
    expect(proveLink(LINK, [], AT, [hit(LINK), empty, ZERO])).toBe("unproven");
  });

  // Splunk refuses an ISO time in latest=; the count the model can actually run
  // names the cause time in epoch seconds.
  it("counts before the cause time when the search names it in epoch seconds", () => {
    const epoch = observation(PROVER_TOOL, `index=main "${LINK}" latest=1704164400 | stats count`, [{ count: 0 }]);
    expect(proveLink(LINK, [], AT, [...BOTH, epoch])).toBe("proven");
    expect(proveOrigin(LINK, [], AT, [epoch])).toBe("proven");
  });

  it("does not take another moment's epoch, or the digits inside a longer number", () => {
    const hourLater = observation(PROVER_TOOL, `"${LINK}" latest=1704168000 | stats count`, [{ count: 0 }]);
    const longer = observation(PROVER_TOOL, `"${LINK}" latest=17041644001 | stats count`, [{ count: 0 }]);
    expect(proveOrigin(LINK, [], AT, [hourLater])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [longer])).toBe("unproven");
  });

  // What invoke journals for splunk_execute: the tool's envelope, with the
  // `| stats count` row inside `results`.
  function wrapped(args: string, inner: readonly unknown[]): Observation {
    return observation(PROVER_TOOL, args, [{ success: true, query: args, count: inner.length, results: inner }]);
  }

  it("reads the count inside the splunk envelope, not the envelope's row count", () => {
    const zero = wrapped(`index=main "${LINK}" latest=1704164400 | stats count`, [{ count: "0" }]);
    expect(proveLink(LINK, [], AT, [...BOTH, zero])).toBe("proven");
    expect(proveLink(LINK, [], AT, [hit(LINK), zero])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [zero])).toBe("proven");
  });

  it("rejects a count above zero inside the envelope", () => {
    const seen = wrapped(`index=main "${LINK}" latest=1704164400 | stats count`, [{ count: "58" }]);
    expect(proveLink(LINK, [], AT, [...BOTH, seen])).toBe("rejected");
    expect(proveOrigin(LINK, [], AT, [seen])).toBe("rejected");
  });

  it("leaves an envelope with no rows, another second, or a longer number unproven", () => {
    const none = wrapped(`index=main "${LINK}" latest=1704164400`, []);
    const hourLater = wrapped(`"${LINK}" latest=1704168000 | stats count`, [{ count: "0" }]);
    const longer = wrapped(`"${LINK}" latest=17041644001 | stats count`, [{ count: "0" }]);
    expect(proveOrigin(LINK, [], AT, [none])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [hourLater])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [longer])).toBe("unproven");
  });

  it("does not let any other telemetry tool prove the count", () => {
    const other = count("splunk_execute", LINK, AT, 0);
    const elastic = count("elastic_search_logs", LINK, AT, 0);
    expect(proveLink(LINK, [], AT, [...BOTH, other])).toBe("unproven");
    expect(proveLink(LINK, [], AT, [...BOTH, elastic])).toBe("unproven");
  });
});

describe("an origin is the same count, one step further", () => {
  it("is proved by a zero count of the link, or of the starting artifact", () => {
    expect(proveOrigin(LINK, [], AT, [ZERO])).toBe("proven");
    expect(proveOrigin("payload.exe", [], AT, [count(PROVER_TOOL, "payload.exe", AT, 0)])).toBe("proven");
  });

  it("is rejected when earlier events carry the value", () => {
    expect(proveOrigin(LINK, [], AT, [count(PROVER_TOOL, LINK, AT, 2)])).toBe("rejected");
  });

  it("is not proved by a count of the actor", () => {
    expect(proveOrigin("alice", ["alice"], AT, [count(PROVER_TOOL, "alice", AT, 0)])).toBe("rejected");
  });
});

describe("a who that is not on a proven step", () => {
  const proven: StepPayload = {
    step_id: "step-1",
    event: "file landed",
    who: "alice",
    at: AT,
    link: LINK,
    artifact: "",
    cause_id: null,
    origin: true,
    link_status: "none",
    origin_status: "proven",
  };
  const open: StepPayload = { ...proven, step_id: "step-2", who: "mallory", origin: false, origin_status: "none" };

  it("is replaced with [unlinked]", () => {
    expect(redact("alice delivered it; mallory was nearby", [proven, open])).toBe(
      "alice delivered it; [unlinked] was nearby",
    );
  });
});
