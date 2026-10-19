import { describe, expect, it } from "vitest";
import { AGENT_ROLE, pluginGrants } from "../../core/agent.js";
import { assembleSpec, loadArch, parseConfig, parsePlaybook } from "../../core/spec.js";
import { archFor } from "../../plugins/kinds.js";
import { pluginFor } from "../../plugins/registry.js";
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

// What the hunt's one agent holds: the deployment's tools bound to the plugin's needs.
const granted = (spec: ReturnType<typeof specWith>) => pluginGrants(pluginFor("hunt"), spec.tools)[AGENT_ROLE];

describe("binding what the hunt needs to what a deployment has", () => {
  it("grants the tool that provides the capability", () => {
    expect(granted(specWith(BASE))).toContain("splunk_search");
  });

  // The case this exists for: a deployment with no telemetry search loses that
  // tool and still runs, rather than failing to build a spec at all.
  it("drops a capability nothing provides rather than refusing the run", () => {
    const withoutSiem = BASE.replace("    provides: telemetry_search\n", "");
    const spec = specWith(withoutSiem);

    expect(granted(spec)).not.toContain("splunk_search");
    expect(granted(spec)).toContain("search_findings");
  });

  // Two deployments, two names, one arch. Nothing in the arch changes.
  it("binds the same capability to whatever this deployment calls it", () => {
    const elastic = BASE.replace("id: splunk_search", "id: elastic_search");
    expect(granted(specWith(elastic))).toContain("elastic_search");
  });

  it("keeps a tool the arch named outright", () => {
    expect(specWith(BASE).roles.lead?.tools).toEqual(["expand"]);
  });
});

// A reader that counts distinct values of an unconstrained string counts a value
// nobody declared as a source of its own. The playbook states the vocabulary and
// the schema enforces it, so a role cannot answer outside it.
describe("constraining a finding's declared vocabulary", () => {
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

  const sourceField = (spec: ReturnType<typeof withDomains>) => dig(spec.finding, "properties", "source_system");

  it("narrows the field to what the playbook declared", () => {
    const spec = withDomains(["net_flow", "dns"]);
    expect(sourceField(spec)["enum"]).toEqual(["net_flow", "dns"]);
  });

  it("leaves the rest of the schema alone", () => {
    const spec = withDomains(["net_flow"]);
    expect(sourceField(spec)["type"]).toBe("string");
    expect(spec.finding?.["required"]).toEqual(["source_system", "summary", "salience", "why_notable", "payload"]);
  });

  // A playbook that declares no vocabulary constrains nothing, which is what
  // every compose run and every legacy hunt does.
  it("constrains nothing when the playbook declared no vocabulary", () => {
    const spec = specWith(BASE);
    expect(sourceField(spec)["enum"]).toBeUndefined();
  });
});

// The same mechanism, a second field: a finding naming a technique nothing in
// the playbook declared is exactly the self-invention the enum exists to stop.
describe("constraining the technique a finding may cite", () => {
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

  const techniqueField = (spec: ReturnType<typeof withTechniques>) => dig(spec.finding, "properties", "attack_technique");

  it("narrows the field to what the playbook declared", () => {
    const spec = withTechniques(["T1071.001", "T1078"]);
    expect(techniqueField(spec)["enum"]).toEqual(["T1071.001", "T1078"]);
  });

  it("leaves the field optional -- a record with nothing to classify says nothing", () => {
    const spec = withTechniques(["T1071.001"]);
    expect(spec.finding?.["required"]).not.toContain("attack_technique");
  });

  it("constrains nothing when the playbook declared no techniques", () => {
    const spec = specWith(BASE);
    expect(techniqueField(spec)["enum"]).toBeUndefined();
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
    expect(dig(spec.finding, "properties", "source_system")["enum"]).toEqual(["net_flow"]);
    expect(techniqueField(spec)["enum"]).toEqual(["T1071.001"]);
  });
});
