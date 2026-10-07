import { describe, expect, it } from "vitest";
import { assembleSpec, loadArch, parseConfig, parsePlaybook } from "../../core/spec.js";
import { archFor } from "../../arch/registry.js";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");

// An arch is versioned and shared; a tool id belongs to a deployment. So the arch
// asks for a capability and the config says what answers it here.
function specWith(configYaml: string) {
  const entry = archFor("hunt");
  return assembleSpec({
    arch: loadArch(entry.arch, entry.actions),
    playbook: parsePlaybook(readFileSync(join(FIXTURES, "hunt.playbook.yaml"), "utf8"), entry.owned),
    config: parseConfig(configYaml, entry.owned),
    prompt: "go",
  });
}

const BASE = readFileSync(join(FIXTURES, "hunt.config.yaml"), "utf8");

describe("binding what a role needs to what a deployment has", () => {
  it("grants the tool that provides the capability", () => {
    const spec = specWith(BASE);
    expect(spec.roles.workers["network_analyst"]?.tools).toContain("splunk_search");
  });

  // The case this exists for: a deployment with no telemetry search loses that
  // tool and still runs, rather than failing to build a spec at all.
  it("drops a capability nothing provides rather than refusing the run", () => {
    const withoutSiem = BASE.replace("    provides: telemetry_search\n", "");
    const spec = specWith(withoutSiem);

    expect(spec.roles.workers["network_analyst"]?.tools).not.toContain("splunk_search");
    expect(spec.roles.workers["threat_hunter"]?.tools).toContain("search_findings");
  });

  // Two deployments, two names, one arch. Nothing in the arch changes.
  it("binds the same capability to whatever this deployment calls it", () => {
    const elastic = BASE.replace("id: splunk_search", "id: elastic_search");
    expect(specWith(elastic).roles.workers["network_analyst"]?.tools).toContain("elastic_search");
  });

  it("keeps a tool the arch named outright", () => {
    expect(specWith(BASE).roles.lead?.tools).toEqual(["expand"]);
  });
});

// A reader that counts distinct values of an unconstrained string counts a value
// nobody declared as a source of its own. The playbook states the vocabulary and
// the schema enforces it, so a role cannot answer outside it.
describe("constraining a worker's declared vocabulary", () => {
  const PLAYBOOK = readFileSync(join(FIXTURES, "hunt.playbook.yaml"), "utf8");

  function withDomains(domains: string[]) {
    const entry = archFor("hunt");
    const declared = domains.map((one) => `  - ${one}`).join("\n");
    return assembleSpec({
      arch: loadArch(entry.arch, entry.actions),
      playbook: parsePlaybook(PLAYBOOK.replace("---\n", `---\ndata_domains:\n${declared}\n`), entry.owned),
      config: parseConfig(BASE, entry.owned),
      prompt: "go",
    });
  }

  const dig = (value: unknown, ...path: string[]): Record<string, unknown> =>
    path.reduce((held, key) => (held as Record<string, unknown>)[key], value) as Record<string, unknown>;

  const sourceField = (spec: ReturnType<typeof withDomains>, worker: string) =>
    dig(spec.roles.workers[worker]?.output_schema, "properties", "results", "items", "properties", "source_system");

  it("narrows the field to what the playbook declared", () => {
    const spec = withDomains(["net_flow", "dns"]);
    expect(sourceField(spec, "network_analyst")["enum"]).toEqual(["net_flow", "dns"]);
  });

  it("narrows it for every worker, not just the one that queries", () => {
    const spec = withDomains(["net_flow"]);
    for (const worker of Object.keys(spec.roles.workers)) {
      expect(sourceField(spec, worker)["enum"]).toEqual(["net_flow"]);
    }
  });

  it("leaves the rest of the schema alone", () => {
    const spec = withDomains(["net_flow"]);
    expect(sourceField(spec, "network_analyst")["type"]).toBe("string");
    const schema = spec.roles.workers["network_analyst"]?.output_schema as Record<string, never>;
    expect(schema["required"]).toEqual(["results"]);
  });

  // A playbook that declares no vocabulary constrains nothing, which is what
  // every compose run and every legacy hunt does.
  it("constrains nothing when the playbook declared no vocabulary", () => {
    const spec = specWith(BASE);
    expect(sourceField(spec, "network_analyst")["enum"]).toBeUndefined();
  });
});

// The same mechanism, a second field: a worker naming a technique nothing in
// the playbook declared is exactly the self-invention the enum exists to stop.
describe("constraining the technique a worker may cite", () => {
  const PLAYBOOK = readFileSync(join(FIXTURES, "hunt.playbook.yaml"), "utf8");

  function withTechniques(techniques: string[]) {
    const entry = archFor("hunt");
    const declared = techniques.map((one) => `  - ${one}`).join("\n");
    return assembleSpec({
      arch: loadArch(entry.arch, entry.actions),
      playbook: parsePlaybook(PLAYBOOK.replace("---\n", `---\nattack_techniques:\n${declared}\n`), entry.owned),
      config: parseConfig(BASE, entry.owned),
      prompt: "go",
    });
  }

  const dig = (value: unknown, ...path: string[]): Record<string, unknown> =>
    path.reduce((held, key) => (held as Record<string, unknown>)[key], value) as Record<string, unknown>;

  const techniqueField = (spec: ReturnType<typeof withTechniques>, worker: string) =>
    dig(spec.roles.workers[worker]?.output_schema, "properties", "results", "items", "properties", "attack_technique");

  it("narrows the field to what the playbook declared", () => {
    const spec = withTechniques(["T1071.001", "T1078"]);
    expect(techniqueField(spec, "network_analyst")["enum"]).toEqual(["T1071.001", "T1078"]);
  });

  it("leaves the field optional -- a record with nothing to classify says nothing", () => {
    const spec = withTechniques(["T1071.001"]);
    const schema = spec.roles.workers["network_analyst"]?.output_schema as Record<string, unknown>;
    const required = dig(schema, "properties", "results", "items")["required"] as string[];
    expect(required).not.toContain("attack_technique");
  });

  it("constrains nothing when the playbook declared no techniques", () => {
    const spec = specWith(BASE);
    expect(techniqueField(spec, "network_analyst")["enum"]).toBeUndefined();
  });

  // The two fields are independent: narrowing one must not disturb the other,
  // which a shared implementation could get wrong by mutating in place.
  it("narrows source_system and attack_technique independently", () => {
    const entry = archFor("hunt");
    const spec = assembleSpec({
      arch: loadArch(entry.arch, entry.actions),
      playbook: parsePlaybook(
        PLAYBOOK.replace("---\n", "---\ndata_domains:\n  - net_flow\nattack_techniques:\n  - T1071.001\n"),
        entry.owned,
      ),
      config: parseConfig(BASE, entry.owned),
      prompt: "go",
    });
    const sourceField = dig(spec.roles.workers["network_analyst"]?.output_schema, "properties", "results", "items", "properties", "source_system");
    expect(sourceField["enum"]).toEqual(["net_flow"]);
    expect(techniqueField(spec, "network_analyst")["enum"]).toEqual(["T1071.001"]);
  });
});
