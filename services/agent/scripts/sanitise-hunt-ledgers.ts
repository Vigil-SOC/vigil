// Rewrites the historical hunt ledgers into fixtures that can be committed.
//
// The ten runs are hunts over BOTS v3, Splunk's published sample dataset, so this
// is hygiene rather than privacy: several hundred AKIA-shaped strings and base64
// payloads would trip secret scanning, and redistributing a third party's dataset
// verbatim is a licence argument nobody needs to have. See the fixtures README.
//
// What the folds read has to survive. Substitution is 1:1 and consistent across
// every run, so entity counts, co-occurrence pair counts and first-sighting are
// unchanged -- which is what hasRarePairing, introducedRecurring and therefore
// salienceFloor are computed from. Seeds, ids and timestamps are carried verbatim,
// so the digest's seeded sampling selects the same records.
//
// What cannot survive: entityViews sorts every entity by bare value across types,
// so which entities land in the digest's window is decided by lexicographic order
// of values that by definition change. Type-shaped pseudonyms cannot sort where
// the originals did -- a URL must start "https://", an AWS key "AKIA". Goldens are
// regenerated from the original implementation over this output, so the gate is
// unaffected; only which values a fixture happens to exercise moves.
//
// Usage: npx tsx scripts/sanitise-hunt-ledgers.ts --in <dir> --out <dir>

import { gunzipSync, gzipSync } from "node:zlib";
import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import { fromText } from "../workflows/hunt/entities.js";

type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

const PAD = 4;
const pad = (index: number): string => String(index + 1).padStart(PAD, "0");

// RFC 1918 and friends. Internal and external are pooled separately so a fixture
// still reads correctly: an external C2 that came back as 10.x would misdescribe
// the beaconing these runs are about.
const isInternalIp = (value: string): boolean => {
  const octets = value.split(".").map(Number);
  const [a, b] = [octets[0] ?? -1, octets[1] ?? -1];
  return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254);
};

// Addresses that name no host: loopback, unspecified, broadcast, multicast. They
// appear all through this data as protocol furniture -- an mDNS query goes to
// 224.0.0.252 -- and renaming them would make the fixture describe traffic that
// could not happen.
const isWellKnownIp = (value: string): boolean => {
  const octets = value.split(".").map(Number);
  const [a] = [octets[0] ?? -1];
  return a === 127 || a >= 224 || value === "0.0.0.0" || value === "255.255.255.255";
};

const ipPool = (prefixes: readonly string[]): string[] => {
  const pool: string[] = [];
  for (const prefix of prefixes) for (let host = 1; host <= 254; host += 1) pool.push(`${prefix}.${host}`);
  return pool.sort();
};

// RFC 5737 documentation blocks, which exist for exactly this. The internal pool
// is 10.128 rather than 10.0: this data contains 10.0.0.5, and a pool that can
// emit an address the input also holds stops being a renaming -- the output would
// carry a real address that merely arrived from somewhere else. Disjointness is
// asserted rather than assumed, below.
const EXTERNAL_POOL = ipPool(["192.0.2", "198.51.100", "203.0.113"]);
const INTERNAL_POOL = ipPool(["10.128.0", "10.128.1", "10.128.2", "10.128.3"]);

const hex = (index: number, length: number): string => (index + 1).toString(16).padStart(length, "0");

// Identity-mapped, because pseudonymising them removes meaning and protects nothing.
// A process here is the attack narrative -- "cat /etc/passwd", "powershell.exe -noni
// -w hidden -c ...", "c:\windows\system32\svchost.exe". Rewriting those to proc0012
// would leave a fixture that records that something ran and nothing about what.
const PRESERVED_TYPES = new Set(["process"]);

// Role accounts, service accounts, and two user-agent strings that typeForKey read as
// users because the field name said "user". None names a person. "system" in
// particular is a common word: substituting it rewrote the key "source_system".
const GENERIC_ACCOUNT = [
  /^nt authority\\/i,
  /^(system|guest|root|administrator|admin|local service|network service|tomcat\d*)$/i,
  /mozilla\//i,
  /^\[/,
];

const isGeneric = (value: string): boolean => GENERIC_ACCOUNT.some((pattern) => pattern.test(value));

// Words that are an account name somewhere and an ordinary technical noun everywhere
// else. "ubuntu" is the default EC2 login and also the OS: derived from the login it
// rewrote "ubuntu 16.04" in a shell transcript. Substituting these costs meaning and
// protects nobody, so a derived token matching one is dropped.
const AMBIGUOUS_TOKEN =
  /^(ubuntu|debian|centos|windows|linux|system|admin|administrator|root|guest|mail|email|postmaster|noreply|no-reply|security|support|info|users?|hosts?|server|client|service|default|public|private|local|remote|master|main|test|temp|data|files?|logs?|events?|alerts?)$/i;

// Keeps the shape a reader needs to recognise the value, and nothing else. Every
// scheme is zero-padded so pseudonym order follows assignment order, which keeps
// neighbours() -- sorted on `type:value` -- ordering the way it did.
const pseudonym = (type: string, value: string, index: number): string => {
  switch (type) {
    // Addresses come from the pools in `collect` and never reach here. Falling
    // through to the default would mint a name that is not an address at all.
    case "ip":
      throw new Error("ip is pooled in collect, not pseudonymised here");
    case "domain":
      return `host${pad(index)}.example.com`;
    case "email":
      return `user${pad(index)}@example.com`;
    // Rebuilt from its host's own pseudonym, by urlPseudonym below.
    case "url":
      throw new Error("url is rebuilt from its host in collect, not pseudonymised here");
    case "hash":
      return hex(index, value.length);
    case "aws_key":
      return `AKIA${pad(index).padStart(16, "0")}`;
    case "arn":
      return `arn:aws:iam::000000000001:user/user${pad(index)}`;
    case "user":
      return value.includes("\\") ? `EXAMPLE\\user${pad(index)}` : `user${pad(index)}`;
    case "process":
      return value.includes("\\") ? `C:\\Windows\\proc${pad(index)}.exe` : `proc${pad(index)}.exe`;
    default:
      return `value${pad(index)}`;
  }
};

// A URL's host is an entity in its own right, so the URL is rebuilt from the name
// that host already carries rather than minting a second one. Minting split one
// real host into two nodes -- and, because both schemes drew from the same
// host{NNNN} counter, it also gave unrelated hosts the same name.
const urlPseudonym = (value: string, index: number, entities: Map<string, string>): string => {
  const parts = /^(https?:\/\/)([^/:?#]+)(:\d+)?/i.exec(value);
  if (parts === null) return `/p${pad(index)}`;
  const [, scheme = "", host = "", port = ""] = parts;
  // Its own namespace, so even a host with no mapping cannot land on a domain's.
  const mapped =
    entities.get(`domain:${host.toLowerCase()}`) ?? entities.get(`ip:${host}`) ?? `url${pad(index)}.example.com`;
  return `${scheme}${mapped}${port}/p${pad(index)}`;
};

// Keys are visited as well as values. The workers emitted payloads whose key names
// embed the value they describe -- "rows_for_FYODOR-L", "dns_answers_matching_<ip>"
// -- so a walk over values alone leaves addresses and hostnames in the tree.
const walk = (node: Json, visit: (node: Json) => void): void => {
  visit(node);
  if (Array.isArray(node)) for (const item of node) walk(item, visit);
  else if (node !== null && typeof node === "object")
    for (const [key, value] of Object.entries(node)) {
      visit(key);
      walk(value, visit);
    }
};

const isEntity = (node: Json): node is { type: string; value: string } & { [key: string]: Json } =>
  node !== null &&
  typeof node === "object" &&
  !Array.isArray(node) &&
  typeof node["type"] === "string" &&
  typeof node["value"] === "string";

// Types the free-text sweep mints pseudonyms for. `url` is excluded: its host or
// address is swept on its own, and replacing a whole URL would take the port and
// path with it -- prose that cites ":8080" would stop matching the evidence.
const SWEPT_TYPES = new Set(["ip", "domain", "email", "hash", "aws_key", "arn"]);

// Kept because they identify a *tool*, not an observation: the enrichment endpoint
// that ran, and the Windows event schema namespace. Removing them would hide which
// integration produced a record, which is the opposite of the point.
const KEPT_DOMAINS = ["threatfox.abuse.ch", "schemas.microsoft.com"];

// A hunt's own case file is `hunt-<id>.case-<id>.md`, and `md` is a TLD, so the
// extractor reads the filename as a hostname. Ids are carried verbatim -- the
// goldens are keyed on them -- so this must not be renamed.
const NOT_A_DOMAIN = /\.md$/i;

// The extractor's notion of an identifier, applied to free text rather than to the
// entity list. The workers wrote whole result tables into payload strings -- a DNS
// answer table naming thirty hosts that were never registered as entities -- and an
// entity-driven pass cannot see any of it.
const sweepable = (type: string, value: string): boolean => {
  if (!SWEPT_TYPES.has(type)) return false;
  if (type === "ip") return !isWellKnownIp(value);
  if (type === "domain") return !NOT_A_DOMAIN.test(value) && !KEPT_DOMAINS.some((kept) => value === kept || value.endsWith(`.${kept}`));
  // The all-zero digest is a "no hash recorded" sentinel, not a file.
  if (type === "hash") return !/^(.)\1+$/.test(value);
  return true;
};

// The BOTS hosts are named <user>-L. They appear bare in prose as often as they do
// as an FQDN, and a bare label is not an entity, so it has to be caught by shape.
const BARE_HOST = /\b[A-Za-z][A-Za-z0-9]{2,}-L\b/g;
const MAC = /\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b/g;
const AWS_ACCOUNT = /\b\d{12}\b/g;
// Any home or user directory, not only the .duckdb these runs happened to name:
// the gate rejects all of them, so the sanitiser has to reach all of them. The
// trailing separator is required -- the analysts write "~" for "approximately".
//
// The `~` branch must also start at a boundary. This pass runs before ENCODED,
// and `~` is what splits the MAPI entry ids below -- so a blob containing "~/"
// was bitten in half here, and the head that survived was short of ENCODED's 40.
// The gate caught it as a path, which is what it looks like once the tail is gone.
const LOCAL_PATH = /(?:(?<![A-Za-z0-9+/~])~\/|(?:\/Users|\/home)\/|C:\\Users\\)[^\s"',;)]*/gi;

// Encoded blobs carry values no substitution can reach -- one of these decodes to
// a real address. `~` is included because the MAPI entry ids in this data are split
// by it, which is what let a blob through when the run had to be unbroken.
const ENCODED = /[A-Za-z0-9+/~]{40,}={0,2}/g;

// Mixed case is what separates base64 from a hash: hex is single-case, so a 64-char
// digest would otherwise be redacted instead of mapped.
const looksEncoded = (text: string): boolean => /[a-z]/.test(text) && /[A-Z]/.test(text);

interface Tables {
  entities: Map<string, string>;
  tokens: Map<string, string>;
}

const collect = (records: Json[]): { entities: Map<string, string>; macs: string[]; hosts: string[] } => {
  const byType = new Map<string, Set<string>>();
  const macs = new Set<string>();
  const hosts = new Set<string>();

  for (const record of records) {
    walk(record, (node) => {
      if (isEntity(node)) {
        const set = byType.get(node.type) ?? new Set<string>();
        set.add(node.value);
        byType.set(node.type, set);
      }
      if (typeof node === "string") {
        for (const mac of node.match(MAC) ?? []) macs.add(mac.toLowerCase());
        for (const host of node.match(BARE_HOST) ?? []) hosts.add(host.toLowerCase());
        for (const found of fromText(node)) {
          if (!sweepable(found.type, found.value)) continue;
          const set = byType.get(found.type) ?? new Set<string>();
          set.add(found.value);
          byType.set(found.type, set);
        }
      }
    });
  }

  // Sorted then zipped against a sorted pool: assignment order is value order, so
  // order within a type is preserved rather than merely deterministic.
  // `url` is assigned last because it is rebuilt from the pseudonym its host
  // already carries, so `domain` and `ip` have to be assigned before it.
  // Alphabetical order happens to give that; leaning on the coincidence does not.
  const entities = new Map<string, string>();
  const last = (type: string): number => (type === "url" ? 1 : 0);
  for (const [type, set] of [...byType.entries()].sort(([a], [b]) => last(a) - last(b) || a.localeCompare(b))) {
    const values = [...set].sort();
    let internal = 0;
    let external = 0;
    // Counts only what is actually renamed, so numbering has no gaps -- and it still
    // rises with sorted order, which is what keeps order within a type preserved.
    let assigned = 0;
    for (const value of values) {
      let mapped: string;
      if (PRESERVED_TYPES.has(type) || isGeneric(value)) {
        mapped = value;
      } else if (type === "ip" && isWellKnownIp(value)) {
        mapped = value;
      } else if (type === "ip") {
        mapped = isInternalIp(value)
          ? (INTERNAL_POOL[internal++] ?? `10.255.255.${internal}`)
          : (EXTERNAL_POOL[external++] ?? `192.0.2.${external}`);
      } else if (type === "url") {
        mapped = urlPseudonym(value, assigned++, entities);
      } else {
        mapped = pseudonym(type, value, assigned++);
      }
      entities.set(`${type}:${value}`, mapped);
    }
  }

  // A pseudonym that is also one of the inputs is not a renaming: the value would
  // still be in the output, arrived at from a different original, and no reading of
  // the fixture could tell. Cheap to check and fatal if true, so it is checked.
  const inputs = new Set([...entities.keys()].map((key) => key.slice(key.indexOf(":") + 1)));
  const collisions = [...entities.entries()].filter(([key, mapped]) => key.slice(key.indexOf(":") + 1) !== mapped && inputs.has(mapped));
  if (collisions.length > 0) throw new Error(`pseudonym collides with an input value: ${collisions.map(([, to]) => to).join(", ")}`);

  return { entities, macs: [...macs].sort(), hosts: [...hosts].sort() };
};

// Bare labels and local parts are derived from the values already mapped, so the
// prose and the entity list agree. Only distinctive labels are taken: mapping a
// generic one like "mail" would rewrite prose that is not about a host at all.
const derivedTokens = (entities: Map<string, string>, hosts: readonly string[]): Map<string, string> => {
  const raw = new Map<string, string>();
  const tokens = {
    set: (from: string, to: string): void => {
      if (!AMBIGUOUS_TOKEN.test(from)) raw.set(from, to);
    },
    has: (from: string): boolean => raw.has(from),
  };

  for (const [key, mapped] of entities) {
    const colon = key.indexOf(":");
    const type = key.slice(0, colon);
    const value = key.slice(colon + 1);

    if (type === "domain") {
      const label = value.split(".")[0] ?? "";
      const distinctive = label.includes("-") || /\d/.test(label) || label.length >= 8;
      if (distinctive && label.length >= 4) {
        const to = mapped.split(".")[0] ?? mapped;
        tokens.set(label, to);
        // The hosts are named <user>-L and the prose quotes the stem alone when it
        // is reciting a SQL pattern it matched on.
        const stem = label.replace(/-l$/i, "");
        if (stem !== label && stem.length >= 5) tokens.set(stem, to.replace(/-l$/i, ""));
      }
    }
    if (type === "email") {
      const local = value.split("@")[0] ?? "";
      const to = mapped.split("@")[0] ?? mapped;
      if (local.length >= 4) tokens.set(local, to);
      // Local parts are <initial><surname> and the surname is used on its own in
      // prose. Only from five characters: "gist" from bgist is also the middle of
      // "registry", and "tun" is too short to be anything but a collision.
      //
      // The guard is on the local part, not the surname. A mailbox that is a word
      // rather than a person yields a word with its head bitten off -- "security"
      // gave "ecurity", which then rewrote the tail of every "security" in the prose.
      if (!AMBIGUOUS_TOKEN.test(local)) {
        const surname = local.slice(1);
        if (/^[a-z]+$/.test(surname) && surname.length >= 5) tokens.set(surname, `${to}-s`);
      }
    }
    if (type === "user") {
      const bare = value.includes("\\") ? (value.split("\\").pop() ?? "") : value;
      if (bare.length >= 5) tokens.set(bare, mapped.includes("\\") ? (mapped.split("\\").pop() ?? mapped) : mapped);
    }
    // The IAM principal an ARN names is quoted bare all through the prose, and it is
    // the account identifier the criteria name -- so it comes off the ARN, not a list.
    if (type === "arn") {
      const principal = value.split("/").pop() ?? "";
      if (principal.length >= 5) tokens.set(principal, mapped.split("/").pop() ?? mapped);
    }
  }

  hosts.forEach((host, index) => {
    if (!tokens.has(host)) tokens.set(host, `host${pad(index)}-l`);
  });

  // The scenario's proper nouns. Explicit because they are prose rather than values
  // -- an analyst wrote "Vultr is a low-cost VPS provider" and "the GitGuardian
  // alert" -- so no shape catches them and no entity mapping reaches them.
  // "budstoll" is a display name no local part yields; it is listed rather than
  // derived, and listed before the surname rule so longest-first prefers it.
  for (const [from, to] of [
    ["frothly", "acme"],
    ["cromdale", "keyword0001"],
    ["gitguardian", "alertvendor"],
    ["vultr", "examplevps"],
    ["brewingiot", "exampleapp"],
    ["budstoll", "user0010-s"],
  ] as const)
    raw.set(from, to);
  return raw;
};

const escape = (text: string): string => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

// One alternation, longest key first, single pass. Sequential replaces would let
// an already-substituted value be rewritten again, and "45.77.53.176" is a prefix
// of the domain "45.77.53.176.vultr.com".
// Entity extraction produces junk as well as entities -- a Sysmon field yielded the
// process "0". Mapping that in the entity list is right, but as a text key it would
// rewrite every zero in the corpus, so the text pass takes only distinctive keys.
const MIN_ENTITY_KEY = 5;
// Tokens are derived or listed rather than extracted, so a short one is deliberate
// -- "btun" and "pcerf" are account identifiers and have to go.
const MIN_TOKEN_KEY = 4;

const compile = (tables: Tables): { pattern: RegExp; lookup: Map<string, string> } => {
  const lookup = new Map<string, string>();
  const keys: string[] = [];
  // An identity mapping is a decision that the value stays, so it must not reach the
  // text pass: "system" maps to itself, and as a text key it rewrites every field
  // name that contains it.
  const take = (key: string, value: string, min: number): void => {
    const lower = key.toLowerCase();
    if (lower === value.toLowerCase()) return;
    lookup.set(lower, value);
    if (lower.length >= min) keys.push(lower);
  };

  for (const [key, value] of tables.entities) take(key.slice(key.indexOf(":") + 1), value, MIN_ENTITY_KEY);
  for (const [key, value] of tables.tokens) take(key, value, MIN_TOKEN_KEY);

  const ordered = [...new Set(keys)].sort((a, b) => b.length - a.length);
  return { pattern: new RegExp(ordered.map(escape).join("|"), "gi"), lookup };
};

const substitute = (text: string, compiled: { pattern: RegExp; lookup: Map<string, string> }): string => {
  const swapped = text.replace(compiled.pattern, (match) => compiled.lookup.get(match.toLowerCase()) ?? match);
  return swapped
    .replace(MAC, (mac) => `02:00:00:00:00:${mac.slice(-2)}`)
    .replace(LOCAL_PATH, (path) => (/\.duckdb$/i.test(path) ? "/fixtures/telemetry.duckdb" : "/fixtures/path"))
    .replace(ENCODED, (run) => (looksEncoded(run) ? "[encoded payload redacted]" : run))
    .replace(AWS_ACCOUNT, "000000000001");
};

const rewrite = (node: Json, compiled: { pattern: RegExp; lookup: Map<string, string> }, entities: Map<string, string>): Json => {
  if (typeof node === "string") return substitute(node, compiled);
  if (Array.isArray(node)) return node.map((item) => rewrite(item, compiled, entities));
  if (node === null || typeof node !== "object") return node;

  // A typed entity is mapped through the table rather than the text pass, so a
  // value the text pass would miss cannot silently survive in the entity list.
  if (isEntity(node)) {
    const mapped = entities.get(`${node.type}:${node.value}`);
    const out: { [key: string]: Json } = {};
    for (const [key, value] of Object.entries(node)) out[key] = rewrite(value, compiled, entities);
    // An identity mapping must not overwrite the text pass. A preserved process still
    // carries values that have to go -- "useradd ... -p davidverve.com" -- and putting
    // the original back would restore the domain the text pass had just removed.
    if (mapped !== undefined && mapped !== node.value) out["value"] = mapped;
    return out;
  }

  const out: { [key: string]: Json } = {};
  for (const [key, value] of Object.entries(node)) out[substitute(key, compiled)] = rewrite(value, compiled, entities);
  return out;
};

const parseArgs = (): { input: string; output: string; review: string | undefined } => {
  const args = process.argv.slice(2);
  const read = (flag: string): string | undefined => {
    const at = args.indexOf(flag);
    return at === -1 ? undefined : args[at + 1];
  };
  const input = read("--in");
  const output = read("--out");
  if (input === undefined || output === undefined) throw new Error("usage: --in <dir> --out <dir> [--review <file>]");
  return { input, output, review: read("--review") };
};

const main = (): void => {
  const { input, output, review } = parseArgs();
  mkdirSync(output, { recursive: true });

  const names = readdirSync(input).filter((name) => name.endsWith(".gz")).sort();
  const ledgers = names.filter((name) => name.endsWith(".jsonl.gz"));
  const torn = names.filter((name) => name.endsWith(".corrupt.gz"));

  const lines = new Map<string, string[]>();
  const parsed: Json[] = [];
  for (const name of [...ledgers, ...torn]) {
    const text = gunzipSync(readFileSync(join(input, name))).toString("utf8");
    const kept = text.split("\n").filter((line) => line.trim() !== "");
    lines.set(name, kept);
    for (const line of kept) {
      try {
        parsed.push(JSON.parse(line) as Json);
      } catch {
        // The torn fixture's last write. It is sanitised as raw text below so the
        // corruption survives -- that record is the whole point of the fixture.
      }
    }
  }

  const { entities, macs, hosts } = collect(parsed);
  const tokens = derivedTokens(entities, hosts);
  const compiled = compile({ entities, tokens });

  for (const [name, kept] of lines) {
    const out = kept.map((line) => {
      try {
        return JSON.stringify(rewrite(JSON.parse(line) as Json, compiled, entities));
      } catch {
        return substitute(line, compiled);
      }
    });
    writeFileSync(join(output, name), gzipSync(`${out.join("\n")}\n`, { level: 9 }));
  }

  // Keyed by digest, not by the value. A plaintext map would restate every value
  // this script exists to remove -- the AKIA ids alone would trip secret scanning
  // and defeat the point. The type and target stay readable so the mapping is
  // still reviewable, and a later run can look its own values up by hashing them.
  // This is not concealment: the originals are a published dataset and the
  // candidate space is small. It stops the values being *restated*, nothing more.
  const digest = (key: string): string => createHash("sha256").update(key).digest("hex").slice(0, 16);
  // The replacement is written as the ledgers carry it, not as the table holds it.
  // An identity mapping -- a preserved process, a role account -- *is* the original,
  // and an original still carries values the text pass removes: the ledgers hold
  // "useradd ... -p host0026.example.com", so the map must not restate the domain
  // that line started out naming. Same reasoning as the identity guard in `rewrite`.
  const table: Record<string, { type: string; to: string }> = {};
  for (const [key, to] of entities)
    table[digest(key)] = { type: key.slice(0, key.indexOf(":")), to: substitute(to, compiled) };
  for (const [key, to] of tokens) table[digest(`token:${key}`)] = { type: "token", to: substitute(to, compiled) };

  const perType: Record<string, number> = {};
  for (const key of entities.keys()) {
    const type = key.slice(0, key.indexOf(":"));
    perType[type] = (perType[type] ?? 0) + 1;
  }

  writeFileSync(
    join(output, "pseudonyms.json"),
    `${JSON.stringify({ note: "sha256(type:value) truncated to 16 hex -> replacement", counts: { ...perType, token: tokens.size, mac: macs.length }, map: table }, null, 2)}\n`,
  );

  // Cleartext, for a human to check the substitutions before goldens are cut from
  // them. Never written into the fixtures directory: this is the file the committed
  // map deliberately is not.
  if (review !== undefined) {
    const rows = [...entities.entries()].map(([key, to]) => `${key}\t-> ${to}${key.endsWith(to) ? "  (kept)" : ""}`);
    const tokenRows = [...tokens.entries()].map(([from, to]) => `token:${from}\t-> ${to}`);
    writeFileSync(review, `${[...rows.sort(), ...tokenRows.sort(), ...macs.map((m) => `mac:${m}`)].join("\n")}\n`);
    console.log(`  review -> ${review}`);
  }

  console.log(`${lines.size} files -> ${output}`);
  console.log(`  ${entities.size} entity values, ${tokens.size} derived tokens, ${macs.length} MACs`);
  for (const name of [...lines.keys()]) console.log(`  ${basename(name)}`);
};

main();
