import { describe, expect, it } from "vitest";
import { timeOf } from "../../../plugins/rootcause/v3/frame.js";

describe("timeOf", () => {
  it("reads epoch seconds as the brief wrote them", () => {
    expect(timeOf("1534759305")).toBe("2018-08-20T10:01:45.000Z");
    expect(timeOf(" 1534759305.5 ")).toBe("2018-08-20T10:01:45.500Z");
  });

  it("reads an ISO timestamp", () => {
    expect(timeOf("2018-08-20T15:17:00Z")).toBe("2018-08-20T15:17:00.000Z");
  });

  it("answers null for no time", () => {
    expect(timeOf(null)).toBeNull();
    expect(timeOf("")).toBeNull();
    expect(timeOf("yesterday-ish")).toBeNull();
  });
});
