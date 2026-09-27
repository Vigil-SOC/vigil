import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor, isHuntLike, registeredKinds } from "../../arch/registry.js";
import { buildSpec, SpecError, type SpecPaths } from "../../core/spec.js";

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
    expect(isHuntLike("root_cause")).toBe(true);
    expect(isHuntLike("adjudicate")).toBe(true);
    expect(isHuntLike("investigate")).toBe(false);
    expect(isHuntLike("compose")).toBe(false);
    expect(isHuntLike("chat")).toBe(false);
    // Registered for nothing at all, so it drives no loop rather than throwing.
    expect(isHuntLike("tally")).toBe(false);
  });
});

describe("the shipped arches", () => {
  it("loads threathunt.yaml unmodified, as a fan-out with a lead", () => {
    const spec = huntSpec();
    expect(spec.arch).toBe("threathunt");
    expect(spec.dispatch).toEqual({ topology: "fan_out", mode: "parallel", fan_out_over: "questions", max_workers: 4 });
    expect(Object.keys(spec.roles.workers)).toEqual(["threat_hunter", "network_analyst", "threat_intel"]);
    expect(spec.roles.critic).toBeDefined();
    expect(spec.digest["evidence_window"]).toBe(25);
  });

  // The hunt loop with an adjudicator's brief: same workers, critic and fan-out as
  // threathunt.yaml, a lead that proposes a workflow and may not hand off to IR.
  it("loads adjudicate.yaml as the hunt fan-out, minus HANDOFF_IR, plus proposed_workflow", () => {
    const spec = buildSpec({ ...HUNT, arch: archFor("adjudicate").arch }, archFor("adjudicate").actions);
    expect(spec.arch).toBe("adjudicate");
    expect(spec.dispatch).toEqual({ topology: "fan_out", mode: "parallel", fan_out_over: "questions", max_workers: 4 });
    expect(Object.keys(spec.roles.workers)).toEqual(["threat_hunter", "network_analyst", "threat_intel"]);
    expect(spec.roles.critic).toBeDefined();
    const properties = spec.roles.lead?.output_schema?.["properties"] as Record<string, { enum?: unknown[] }>;
    expect(properties["action"]?.enum).not.toContain("HANDOFF_IR");
    expect(properties["action"]?.enum).toContain("CONCLUDE");
    expect(properties["proposed_workflow"]).toEqual({ type: ["string", "null"] });
    expect(spec.roles.lead?.output_schema?.["required"]).not.toContain("proposed_workflow");
    expect(spec.roles.lead?.prompt).toContain("You execute nothing");
  });

  // The other shape the indirection has to carry: one role, its tools, no fan-out.
  it("loads investigate.yaml as a single lead with no workers and no critic", () => {
    const spec = buildSpec(CASE, archFor("investigate").actions);
    expect(spec.arch).toBe("investigate");
    expect(spec.dispatch).toEqual({ topology: "single", mode: "serial", fan_out_over: "questions", max_workers: 1 });
    expect(spec.roles.workers).toEqual({});
    expect(spec.roles.critic).toBeUndefined();
    expect(spec.roles.lead?.tools).toEqual(["case_records", "get_finding"]);
  });

  it("generates the roster from the worker registry and narrows worker_agent_id to it", () => {
    const spec = huntSpec();
    const properties = spec.roles.lead?.output_schema?.["properties"] as Record<string, { enum?: unknown[] }>;
    expect(spec.roles.lead?.prompt).toContain("- network_analyst — traffic shape");
    expect(properties["worker_agent_id"]?.enum).toEqual(["threat_hunter", "network_analyst", "threat_intel", null]);
  });

  // A single-lead arch has no roster to generate and nothing to narrow it to.
  it("leaves the lead schema alone when the arch declares no workers", () => {
    const spec = buildSpec(CASE, archFor("investigate").actions);
    expect(spec.roles.lead?.prompt).not.toContain("Workers you may dispatch");
    expect(spec.roles.lead?.output_schema?.["properties"]).not.toHaveProperty("worker_agent_id");
  });

  it("layers playbook directives onto the arch prompts rather than replacing them", () => {
    const spec = huntSpec();
    expect(spec.roles.lead?.prompt).toContain("You are the Hunt Lead");
    expect(spec.roles.workers["threat_intel"]?.prompt).toContain("snapshotted on the first day");
    expect(spec.roles.workers["network_analyst"]?.prompt).toContain("seven-day export");
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
    expect(() => loadArchOnly(archOf("dispatch: { mode: serial }", "FLY"))).toThrow(
      /roles\.lead declares action\(s\) no workflow handles: FLY/,
    );
  });

  it("rejects a lead with no action enum", () => {
    const body = "name: n\nroles:\n  lead:\n    prompt: Decide.\n    output_schema: { type: object }\n";
    expect(() => loadArchOnly(body)).toThrow(/needs a non-empty action enum/);
  });

  // mode is checked rather than coerced, so it is not a field nothing reads.
  it("rejects a topology nothing implements, at load rather than at run", () => {
    expect(() => loadArchOnly(archOf("dispatch: { topology: hive }"))).toThrow(/no topology hive/);
  });

  it("rejects single declaring workers it would never dispatch to", () => {
    const body = archOf("dispatch: { topology: single }").concat(
      "\n  workers:\n    scout:\n      description: looks\n      prompt: Look.\n      output_schema: { type: object }",
    );
    expect(() => loadArchOnly(body)).toThrow(/single dispatches to nobody/);
  });

  it("rejects fan_out with no workers to fan out to", () => {
    expect(() => loadArchOnly(archOf("dispatch: { topology: fan_out }"))).toThrow(/needs workers/);
  });

  it("defaults to single when an arch declares no workers", () => {
    expect(loadArchOnly(archOf("")).dispatch.topology).toBe("single");
  });

  it("rejects a dispatch mode that contradicts its worker count", () => {
    expect(() => loadArchOnly(archOf("dispatch: { mode: serial, max_workers: 3 }"))).toThrow(
      /dispatch\.mode serial is max_workers 1, so 3 contradicts it/,
    );
  });

  it("rejects a role granted a tool the config does not declare", () => {
    const body = archOf("dispatch: { mode: serial }").replace("  lead:\n", "  lead:\n    tools: [nowhere]\n");
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

  it("refuses a config that names no model", () => {
    const config = scratchFile("no-model.yaml", "budgets: { max_calls: 2 }\n");
    expect(() => buildSpec({ ...CASE, config }, ["EXAMINE", "CONCLUDE"])).toThrow(/config needs a model/);
  });
});
