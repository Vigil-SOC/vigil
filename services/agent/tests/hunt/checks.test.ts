import { describe, expect, it } from "vitest";
import type { CallPayload } from "../../contracts/events.js";
import { carriesField, checkValues, rowsOf, sameField } from "../../plugins/hunt/checks.js";

// A record from a real hunt, checked against the query it cited: a Splunk stats over the
// host's flows, its rows wrapped in the tool's {success, query, results} answer.
const r6: CallPayload = {
  ref: "r6",
  turn: 3,
  call_id: "c6",
  tool: "splunk_execute",
  arguments: "{}",
  ok: true,
  failure: null,
  result: [
    '<vigil:tool_result tool="splunk_execute" ref="r6">',
    "1 row(s) from splunk",
    JSON.stringify([
      {
        success: true,
        results: [
          { dest_ip: "45.77.53.176", dest_port: "443", count: "3884", bytes_out: "1441679", bytes_in: "1755887" },
          { dest_ip: "192.168.9.30", dest_port: "80", count: "32", bytes_out: "4440", bytes_in: "9459" },
        ],
      },
    ]),
    "</vigil:tool_result>",
  ].join("\n"),
} as CallPayload;

const payload = {
  window: "2018-08-20T10:00-12:00Z",
  dest_ip: "45.77.53.176",
  dest_port: 443,
  conn_count: 3885,
  agg_bytes_in: 1755887,
  ssl_versions: ["0.0", "3.1"],
  agg_bytes_out: 1441679,
  next_busiest_dest_conn: 32,
};

describe("a record's values checked against the result it cites", () => {
  const checks = Object.fromEntries(checkValues(payload, [r6]).map((check) => [check.field, check]));

  it("reads the rows out of a wrapped answer", () => {
    expect(rowsOf(r6.result)).toHaveLength(2);
  });

  it("finds a value under the result's own name for it", () => {
    expect(checks["agg_bytes_out"]).toMatchObject({ status: "match", ref: "r6", found_field: "bytes_out" });
    expect(checks["dest_port"]).toMatchObject({ status: "match", found_field: "dest_port" });
  });

  it("says a value differs when the row it is about holds another", () => {
    expect(checks["conn_count"]).toMatchObject({ status: "differs", found_field: "count", found: "3884" });
  });

  it("finds a distinctive value under an unrelated name", () => {
    expect(checks["next_busiest_dest_conn"]).toMatchObject({ status: "match", found_field: "count", found: "32" });
  });

  it("says a value the result does not carry is not in it, rather than guessing", () => {
    expect(checks["window"]!.status).toBe("absent");
    expect(checks["ssl_versions"]!.status).toBe("absent");
  });

  it("names a field by its parts and its plural", () => {
    expect(sameField("conn_count", "count")).toBe(true);
    expect(sameField("ssl_versions", "ssl_version")).toBe(true);
    expect(sameField("dest_ip", "src_ip")).toBe(false);
    expect(carriesField("bytes_out", [r6])).toBe(true);
    expect(carriesField("ssl_version", [r6])).toBe(false);
  });

  it("takes a number rounded to the places the record wrote as a match, not a difference", () => {
    const r9 = {
      ...r6,
      ref: "r9",
      result: ['<vigil:tool_result tool="splunk_execute" ref="r9">', "1 row(s) from splunk", JSON.stringify([{ avg_per_min: "33.48275862068966", sd: "8.791777540624986", mn: "3", mx: "48" }]), "</vigil:tool_result>"].join("\n"),
    } as CallPayload;
    const found = Object.fromEntries(checkValues({ avg_per_min: 33.5, sd_per_min: 8.79, min_per_min: 3, max_per_min: 48, dest_ip: "45.77.53.176", conn_count: 3885 }, [r6, r9]).map((check) => [check.field, check]));
    expect(found["avg_per_min"]).toMatchObject({ status: "match", rounded: true, ref: "r9" });
    expect(found["sd_per_min"]).toMatchObject({ status: "match", found_field: "sd", rounded: true });
    expect(found["min_per_min"]).toMatchObject({ status: "match", found_field: "mn" });
    expect(found["max_per_min"]).toMatchObject({ status: "match", found_field: "mx" });
    // An integer one off is still a difference: rounding never moves a whole number.
    expect(found["conn_count"]!.status).toBe("differs");
  });

  it("takes a number with its fraction cut off as a match: an epoch written without its milliseconds", () => {
    const r8 = {
      ...r6,
      ref: "r8",
      result: ['<vigil:tool_result tool="splunk_execute" ref="r8">', "1 row(s) from splunk", JSON.stringify([{ dest_ip: "45.77.53.176", first: "1534759305.832", last: "1534766375.226" }]), "</vigil:tool_result>"].join("\n"),
    } as CallPayload;
    const found = Object.fromEntries(checkValues({ dest_ip: "45.77.53.176", first: 1534759305, last: 1534766375 }, [r8]).map((check) => [check.field, check]));
    expect(found["first"]).toMatchObject({ status: "match", rounded: true });
    expect(found["last"]).toMatchObject({ status: "match", rounded: true });
    // Neither cut nor rounded: still a difference.
    const off = checkValues({ dest_ip: "45.77.53.176", first: 1534759307 }, [r8]).find((check) => check.field === "first");
    expect(off?.status).toBe("differs");
  });
});
