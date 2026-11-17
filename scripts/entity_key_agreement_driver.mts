// The harness half of scripts/check_entity_key_agreement.py: runs the hunt
// harness's Entity Key code on the inputs the check hands over (JSON on stdin)
// and prints what it did (JSON on stdout). It holds no inputs and no answers.
// Run from services/agent so `tsx` resolves; see the check for why.
import { readFileSync } from "node:fs";

type Module = Record<string, unknown>;

const ROOT = new URL("../", import.meta.url);

// Every entry point the check reaches the harness through, by module.
const ENTRY_POINTS: Record<string, string[]> = {
  "services/agent/plugins/hunt/entities.ts": ["parseKey", "fromText", "defang"],
  "services/agent/plugins/hunt/types.ts": ["ENTITY_TYPES"],
  "services/agent/plugins/hunt/tlds.ts": ["TLDS"],
  "services/agent/contracts/memory.ts": ["KEY_CASE_SENSITIVE_TYPES"],
  "clients/web/src/screens/workflows/entityTypes.ts": ["ENTITY_TYPES"],
};

async function main(): Promise<void> {
  const input = JSON.parse(readFileSync(0, "utf8")) as {
    subjects: string[];
    texts: string[];
    defangs: string[];
  };
  const modules: Record<string, Module> = {};
  const missing: string[] = [];
  for (const [path, names] of Object.entries(ENTRY_POINTS)) {
    try {
      modules[path] = (await import(new URL(path, ROOT).href)) as Module;
    } catch (error) {
      missing.push(`${names.join(", ")} (${path} would not load: ${error instanceof Error ? error.message : error})`);
      continue;
    }
    for (const name of names) if (modules[path]?.[name] === undefined) missing.push(`${name} (${path})`);
  }
  const node = process.version;
  if (missing.length > 0) {
    console.log(JSON.stringify({ node, missing }));
    return;
  }

  const entities = modules["services/agent/plugins/hunt/entities.ts"] as {
    parseKey(raw: string): { type: string; value: string } | undefined;
    fromText(raw: string): { type: string; value: string }[];
    defang(text: string): string;
  };
  const list = (path: string, name: string): string[] => [...((modules[path] as Module)[name] as Iterable<string>)];
  const keyOf = (e: { type: string; value: string }): string => `${e.type}:${e.value}`;

  console.log(
    JSON.stringify({
      node,
      missing,
      lists: {
        entity_types: list("services/agent/plugins/hunt/types.ts", "ENTITY_TYPES"),
        console_entity_types: list("clients/web/src/screens/workflows/entityTypes.ts", "ENTITY_TYPES"),
        case_sensitive_types: list("services/agent/contracts/memory.ts", "KEY_CASE_SENSITIVE_TYPES"),
        tlds: list("services/agent/plugins/hunt/tlds.ts", "TLDS"),
      },
      // The harness's own keys, as it writes them: no re-normalising afterwards.
      subjects: input.subjects.map((raw) => {
        const entity = entities.parseKey(raw);
        return entity === undefined ? null : keyOf(entity);
      }),
      texts: input.texts.map((raw) => entities.fromText(raw).map(keyOf)),
      defangs: input.defangs.map((raw) => entities.defang(raw)),
    }),
  );
}

await main();
