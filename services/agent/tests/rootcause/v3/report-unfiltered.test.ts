// The unfiltered report (for grading without report.ts's grader-shaped filters) leaves report.md unchanged:
// the golden file was rendered by the code before the unfiltered mode existed.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { renderReport } from "../../../plugins/rootcause/v3/report.js";
import { alert, reportFixture } from "./fixtures/report-fixture.js";

describe("unfiltered report", () => {
  it("report.md is byte-identical to the one rendered before the change", () => {
    const { g, trace } = reportFixture();
    expect(renderReport(g, trace, null, alert)).toBe(readFileSync(new URL("./fixtures/report-golden.md", import.meta.url), "utf8"));
  });
  it("report-unfiltered.md keeps what the filters drop and adds stories and reasoning", () => {
    const { g, trace } = reportFixture();
    const md = renderReport(g, trace, null, alert, undefined, { unfiltered: true });
    expect(md).not.toMatch(/not the attacker`?$|legitimate user; its activity|victim account, used by/m);
    expect(md).toMatch(/- the attacker \(an operator the logs do not name\): attacker/);
    expect(md).toMatch(/cannot come out benign/);
    expect(md).toMatch(/- ip: 192\.0\.2\.7, 198\.51\.100\.9, 203\.0\.113\.5/); // legitVals and islands filters off
    expect(md).toMatch(/- user: devuser2/);
    expect(md).toMatch(/\(edr, found\) devuser pushed the config/); // names the legitimate user
    expect(md).toMatch(/\(edr, not found\) the attacker left no file/); // accusing wording
    expect(md).toMatch(/\*\*R1\.H1\*\* \(round 1, replaced[^\n]*an outsider took/);
    expect(md).toMatch(/\*\*R2\.H1\*\* \(round 2, live[^\n]*devuser is the attacker/);
    expect(md).toMatch(/- turn 3 \(thinking\): the session is j\.doe's but the address is new/);
    expect(md).toMatch(/- turn 5 \(submit_causes\): devuser's config push came right before/);
    expect(md).toMatch(/\*\*N2\*\* devuser applied a config from 192\.0\.2\.7 — [^\n]*← link/);
    expect(md).not.toMatch(/legitimate activity, not the attacker/);
    expect(md).toMatch(/ · legitimate activity · ran as devuser/); // the timeline keeps the model's flag
  });
});
