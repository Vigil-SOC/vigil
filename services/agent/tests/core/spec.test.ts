import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor, isHuntLike, registeredKinds } from "../../plugins/kinds.js";
import { buildSpec, parseConfig, SpecError, type SpecPaths } from "../../core/spec.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const scratch = mkdtempSync(join(tmpdir(), "vigil-arch-"));

function fixture(name: string): string {
  return join(FIXTURES, name);
}

function scratchFile(name: string, body: string): string {
  const path = join(scratch, name);
  writeFileSync(path, body, "utf8");
  return path;
}

const HUNT: SpecPaths = { arch: archFor("hunt").arch, playbook: fixture("hunt.playbook.yaml"), config: fixture("hunt.config.yaml") };
const CASE: SpecPaths = {
  arch: archFor("investigate").arch,
  playbook: fixture("case.playbook.yaml"),
  config: fixture("case.config.yaml"),
};

function huntSpec() {
  return buildSpec(HUNT, archFor("hunt").actions);
}

// A minimal arch, so a test that is about one key is not also about the rest.
function archOf(extra: string, action = "EXAMINE"): string {
  return [
    "name: minimal",
    extra,
    "roles:",
    "  lead:",
    "    prompt: Decide.",
    "    output_schema:",
    "      type: object",
    "      properties:",
    `        action: { type: string, enum: [${action}] }`,
  ].join("\n");
}

function loadArchOnly(body: string, handled: readonly string[] = ["EXAMINE", "CONCLUDE"]) {
  return buildSpec({ ...CASE, arch: scratchFile(`arch-${Math.random().toString(36).slice(2)}.yaml`, body) }, handled);
}

describe("the registry resolves a run kind to an arch", () => {
  it("registers the shipped arches and nothing else", () => {
    expect(registeredKinds()).toEqual(["adjudicate", "chat", "compose", "hunt", "investigate", "root_cause"]);
  });

  // Adding an agent type is an arch file and an entry. Nothing in the worker
  // names a kind, so a new one reaches its loop without a branch being added.
  it("names the loop that drives each kind, rather than leaving the worker to switch", () => {
    expect(archFor("hunt").workflow).toBe("hunt");
    expect(archFor("root_cause").workflow).toBe("rootcause");
    expect(archFor("investigate").workflow).toBe("lead");
    expect(archFor("compose").workflow).toBe("compose");
  });

  // A kind in the union with no arch behind it is the failure this prevents.
  it("refuses a run kind nothing is registered for", () => {
    expect(() => archFor("tally")).toThrow(/no architecture is registered for run_kind tally/);
  });

  // Read off the entry rather than listed anywhere, so a kind that shares the hunt
  // loop is hunt-like by having been registered with it. Everything that gates on
  // "is this the hunt loop?" asks this, which is what stops one caller being widened
  // for a new kind and the next three being found later, one bug at a time.
  it("answers which kinds run the hunt loop from what they were registered with", () => {
    expect(isHuntLike("hunt")).toBe(true);
    expect(isHuntLike("root_cause")).toBe(false);
    expect(isHuntLike("adjudicate")).toBe(true);
    expect(isHuntLike("investigate")).toBe(false);
    expect(isHuntLike("compose")).toBe(false);
    expect(isHuntLike("chat")).toBe(false);
    // Registered for nothing at all, so it drives no loop rather than throwing.
    expect(isHuntLike("tally")).toBe(false);
  });
});

describe("the arch layer each plugin.yaml carries", () => {
  // The allowance is the turn and call ceilings the retired fan-out arithmetic produced
  // ((4 workers + 2) turns, times 8 + 2 calls each); a hunt runs to exactly these.
  it("reads the hunt's as a lead and a critic, with its allowance and finding shape", () => {
    const spec = huntSpec();
    expect(spec.arch).toBe("hunt");
    expect(spec.allowance).toEqual({ turns_per_iteration: 6, calls_per_iteration: 60 });
    expect(Object.keys(spec.roles)).toEqual(["lead", "critic"]);
    expect(spec.finding?.["required"]).toEqual(["source_system", "summary", "salience", "why_notable", "payload"]);
    expect(spec.roles.lead?.tools).toEqual(["expand"]);
    expect(spec.roles.critic?.prompt).toContain("disconfirmation critic");
    expect(spec.roles.critic?.output_schema?.["required"]).toContain("benign_explanation_stands");
  });

  // An adjudication is a hunt run as a second opinion: the same allowance, finding and critic.
  it("reads the adjudicator's as the hunt's", () => {
    const hunt = huntSpec();
    const spec = buildSpec({ ...HUNT, arch: archFor("adjudicate").arch }, archFor("adjudicate").actions);
    expect(spec.arch).toBe("adjudicate");
    expect(spec.allowance).toEqual(hunt.allowance);
    expect(spec.finding).toEqual(hunt.finding);
    expect(spec.roles.critic).toBeDefined();
  });

  // The other shape the indirection has to carry: one role, its tools, nothing to count.
  it("reads root cause's as one investigator with no tools of its own, answering in prose", () => {
    const spec = buildSpec(
      {
        arch: archFor("root_cause").arch,
        playbook: fixture("case.playbook.yaml"),
        config: scratchFile(
          "rootcause.config.yaml",
          [
            "model: scripted/model",
            "budgets: { max_calls: 32, max_cost_usd: 15, max_wall_ms: 5400000 }",
            "runtime: { max_turns: 32, result_cap: 8000, recall_limit: 1 }",
            "tools: []",
            "approvals: []",
          ].join("\n"),
        ),
      },
      archFor("root_cause").actions,
      archFor("root_cause").owned,
    );
    expect(spec.arch).toBe("rootcause");
    expect(spec.allowance).toBeUndefined();
    expect(spec.roles.critic).toBeUndefined();
    expect(spec.roles.lead?.output_schema).toBeNull();
    expect(spec.roles.lead?.tools).toEqual([]);
    expect(spec.roles.lead?.needs).toEqual(["telemetry_search"]);
  });

  it("reads investigate's as a single lead and no critic", () => {
    const spec = buildSpec(CASE, archFor("investigate").actions);
    expect(spec.arch).toBe("investigate");
    expect(spec.finding).toBeUndefined();
    expect(spec.roles.critic).toBeUndefined();
    expect(spec.roles.lead?.needs).toEqual([
      "case_records", "get_finding", "telemetry_search", "findings_search", "indicator_lookup",
    ]);
  });

  it("layers playbook directives onto the roles rather than replacing them", () => {
    const spec = huntSpec();
    expect(spec.roles.lead?.prompt).toContain("seven-day export");
    expect(spec.roles.critic?.prompt).toContain("disconfirmation critic");
    expect(spec.roles.critic?.prompt).toContain("snapshotted on the first day");
    expect(spec.name).toBe("beaconing on the finance segment");
    expect(spec.narrative).toContain("stays busy overnight");
  });
});

describe("the three files are disjoint", () => {
  it("sends a config key found in an arch to the config file", () => {
    expect(() => loadArchOnly(archOf("budgets: { max_calls: 4 }"))).toThrow(
      /budgets belongs in the config file, not the arch file/,
    );
  });

  it("sends an arch key found in a playbook to the arch file", () => {
    const playbook = scratchFile("stray.playbook.yaml", "name: stray\nroles: { lead: {} }\n");
    expect(() => buildSpec({ ...CASE, playbook }, ["EXAMINE", "CONCLUDE"])).toThrow(
      /roles belongs in the arch file, not the playbook file/,
    );
  });

  it("sends a playbook key found in a config to the playbook file", () => {
    const config = scratchFile("stray.config.yaml", "model: m\nobjectives: [find it]\n");
    expect(() => buildSpec({ ...CASE, config }, ["EXAMINE", "CONCLUDE"])).toThrow(
      /objectives belongs in the playbook file, not the config file/,
    );
  });

  // No precedence chain: a key in none of the three is a typo, not a default.
  it("says so when a key belongs in no file at all", () => {
    expect(() => loadArchOnly(archOf("temperature: 0.2"))).toThrow(/temperature belongs in no file/);
  });
});

describe("the loader refuses an arch it could not honour", () => {
  it("rejects an action no workflow handles", () => {
    expect(() => loadArchOnly(archOf("", "FLY"))).toThrow(
      /roles\.lead declares action\(s\) no workflow handles: FLY/,
    );
  });

  it("rejects a lead with no action enum", () => {
    const body = "name: n\nroles:\n  lead:\n    prompt: Decide.\n    output_schema: { type: object }\n";
    expect(() => loadArchOnly(body)).toThrow(/needs a non-empty action enum/);
  });

  // Worker roles and a dispatch policy went with the workers; an arch still naming
  // them is refused rather than read as something nothing honours.
  it("refuses the retired worker roles and dispatch policy", () => {
    expect(() => loadArchOnly(archOf("dispatch: { mode: parallel }"))).toThrow(/dispatch belongs in no file/);
    const body = archOf("").concat("\n  workers:\n    scout:\n      prompt: Look.");
    expect(() => loadArchOnly(body)).toThrow(/unknown role group\(s\): workers/);
  });

  it("refuses an allowance that is not two positive integers", () => {
    expect(() => loadArchOnly(archOf("allowance: { turns_per_iteration: 0, calls_per_iteration: 60 }"))).toThrow(
      /allowance\.turns_per_iteration must be a positive integer, got 0/,
    );
    expect(() => loadArchOnly(archOf("allowance: { workers: 4 }"))).toThrow(/unknown allowance key\(s\): workers/);
  });

  it("rejects a role granted a tool the config does not declare", () => {
    const body = archOf("").replace("  lead:\n", "  lead:\n    tools: [nowhere]\n");
    expect(() => loadArchOnly(body)).toThrow(/arch role lead needs tool\(s\) the config does not declare: nowhere/);
  });

  it("rejects a playbook directive naming a role the arch does not declare", () => {
    const playbook = scratchFile("bad-directive.yaml", "name: p\ndirectives: { forensics: look here }\n");
    expect(() => buildSpec({ ...CASE, playbook }, ["EXAMINE", "CONCLUDE"])).toThrow(
      /directives name unknown role\(s\): forensics/,
    );
  });

  it("reports the path of the file it could not read", () => {
    const missing = { ...CASE, config: "/nowhere/vigil.config.yaml" };
    expect(() => buildSpec(missing, archFor("investigate").actions)).toThrow(SpecError);
    expect(() => buildSpec(missing, archFor("investigate").actions)).toThrow(/no such config file: \/nowhere\/vigil.config.yaml/);
  });
});

describe("the config layer", () => {
  it("refuses an approval naming a tool it does not declare", () => {
    const config = scratchFile("bad-approval.yaml", "model: m\ntools: [{ id: a, kind: k }]\napprovals: [b]\n");
    expect(() => buildSpec({ ...CASE, config }, ["EXAMINE", "CONCLUDE"])).toThrow(/approvals name tool\(s\).*: b/);
  });

  it("refuses an unknown budget key rather than defaulting past it", () => {
    const config = scratchFile("bad-budget.yaml", "model: m\nbudgets: { max_dollars: 4 }\n");
    expect(() => buildSpec({ ...CASE, config }, ["EXAMINE", "CONCLUDE"])).toThrow(/unknown budgets key\(s\): max_dollars/);
  });

  it("reads a context window, and refuses one that is not a non-negative integer", () => {
    expect(parseConfig("model: m\ncontext_window: 16384\n").context_window).toBe(16384);
    expect(parseConfig("model: m\n").context_window).toBeUndefined();
    for (const bad of ["-1", "1.5", "big"]) {
      expect(() => parseConfig(`model: m\ncontext_window: ${bad}\n`)).toThrow(/context_window must be a non-negative integer/);
    }
  });

  it("refuses a config that names no model", () => {
    const config = scratchFile("no-model.yaml", "budgets: { max_calls: 2 }\n");
    expect(() => buildSpec({ ...CASE, config }, ["EXAMINE", "CONCLUDE"])).toThrow(/config needs a model/);
  });
});
