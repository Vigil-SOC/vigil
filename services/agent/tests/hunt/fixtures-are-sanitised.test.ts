import { describe, expect, it } from "vitest";
import { gunzipSync } from "node:zlib";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fromPayload, fromText } from "../../workflows/hunt/entities.js";

// The ledgers under tests/fixtures/runs are hunts over BOTS v3 rewritten by
// scripts/sanitise-hunt-ledgers.ts. This is the gate on that rewrite: it asserts
// the shape of what is committed rather than trusting the script that produced it,
// so a fixture added later cannot bring a real address back with it.
//
// Identifiers are found with the pipeline's own extractor, not a private regex --
// whatever the fold would read as an entity is what this holds to the allowlist.
// Both halves of the extractor are used, because they see different things:
// fromText over the raw bytes reads values no payload key ever typed, and
// fromPayload over the parsed documents is the only one that reads a `user` at
// all -- fromText has no pattern that can yield one.

const RUNS = join(import.meta.dirname, "..", "fixtures", "runs");

// Recorded by current code rather than rewritten from a real hunt, so its
// identifiers are invented -- and swept anyway: a fixture is committed once and
// read forever, and the sweep is what makes that safe whatever wrote it.
const REPLAY = join(import.meta.dirname, "..", "fixtures", "replay");

// RFC 5737 documentation blocks, the internal pool, and addresses that name no
// host -- loopback, unspecified, broadcast, multicast.
// `::` and `::1` are here for the same reason: the extractor types them as
// addresses, and neither names a host.
const ALLOWED_IP = /^(?:192\.0\.2\.\d{1,3}|198\.51\.100\.\d{1,3}|203\.0\.113\.\d{1,3}|10\.128\.[0-3]\.\d{1,3}|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|0\.0\.0\.0|255\.255\.255\.255|2(?:2[4-9]|3\d)\.\d{1,3}\.\d{1,3}\.\d{1,3}|::1?)$/;

// example.com per RFC 2606, plus the two domains that name a tool rather than an
// observation: the enrichment endpoint that ran and the Windows event schema.
// `.md` is a real TLD and a hunt's case file is `hunt-<id>.case-<id>.md`, which
// the extractor reads as a hostname; ids are carried verbatim by design.
const ALLOWED_DOMAIN = /(?:^|\.)example\.com$|^threatfox\.abuse\.ch$|^schemas\.microsoft\.com$|\.md$/;

// The two pseudonym shapes, plus the role and service accounts the sanitiser
// preserves on purpose. None of these names a person, and a real login -- bare or
// domain-qualified -- fails every branch.
const ALLOWED_USER =
  /^(?:example\\)?user\d{4}$|^nt authority\\|^(?:system|guest|root|administrator|admin|local service|network service|tomcat\d*)$|^mozilla\/|^\[/i;

const ALLOWED = new Map<string, RegExp>([
  ["ip", ALLOWED_IP],
  ["domain", ALLOWED_DOMAIN],
  ["email", /@example\.com$/],
  // Pseudonymous digests are the assignment index, zero-padded to the original
  // length, so a real digest fails on entropy alone.
  ["hash", /^0+[0-9a-f]{0,6}$/],
  ["aws_key", /^AKIA0{12}\d{4}$/],
  ["arn", /^arn:aws:iam::000000000001:user\/user\d{4}$/],
  ["user", ALLOWED_USER],
]);

// A URL's host and address are extracted in their own right, so the rules above
// already cover it; its port and path are deliberately left alone.
// A process survives verbatim by design -- it is the attack narrative -- so there
// is no shape to hold it to. An identifier embedded in one is not unchecked: the
// sweeps below read the same bytes as free text and catch it there.
const UNCHECKED = new Set(["url", "process"]);

const SHAPES: readonly [string, RegExp, RegExp][] = [
  ["MAC address", /\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b/gi, /^02:00:00:00:00:[0-9a-f]{2}$/i],
  // A ledger id is twelve hex characters and lands on all-digits often enough to
  // matter -- dec-131531207758 is a decision id, not an account. Exempted only
  // behind the id prefixes the ledger itself writes, so a bare twelve-digit run
  // still fails. This does trade something away: a real account id written as
  // `run-<id>` would now pass, which no ledger writes and no sanitiser produces --
  // an account reaches a ledger bare or inside an ARN, and both still fail.
  ["AWS account id", /(?<!\b(?:dec|dsp|hyp|ev|chk|dir|hunt|run|q)-)\b\d{12}\b/g, /^000000000001$/],
  ["bare BOTS hostname", /\b[A-Za-z][A-Za-z0-9]{2,}-L\b/g, /^host\d{4}-l$/i],
  // A trailing separator is required: the analysts write "~" for "approximately"
  // all through the prose, and "~6s-interval" is not a home directory.
  // `/fixtures/` is part of the shape so the allowance is a live rule rather than
  // an unreachable one: it is what the sanitiser rewrites every local path to.
  ["local filesystem path", /(?:(?:~|\/Users|\/home)\/|C:\\Users\\|\/fixtures\/)[^\s"',;)]*/gi, /^\/fixtures\//],
];

// Mixed case over a long unbroken run is base64, and one such blob decoded to a
// real address. Single-case runs are digests and are checked as hashes.
const ENCODED = /[A-Za-z0-9+/~]{40,}={0,2}/g;

const files = readdirSync(RUNS).filter((name) => name.endsWith(".gz")).sort();
const replayed = readdirSync(REPLAY).filter((name) => name.endsWith(".gz")).sort();

// pseudonyms.json is swept with the ledgers. It is committed beside them and its
// values are drawn from the same runs, so it is exactly as able to carry something
// real -- and it is the one file the rewrite does not itself pass through.
const PSEUDONYMS = "pseudonyms.json";
const swept = [...files, PSEUDONYMS, ...replayed];

const dirOf = (name: string): string => (replayed.includes(name) ? REPLAY : RUNS);

const textOf = (name: string): string =>
  name === PSEUDONYMS
    ? readFileSync(join(RUNS, name), "utf8")
    : gunzipSync(readFileSync(join(dirOf(name), name))).toString("utf8");

// Parsed, so the payload-typed entities can be read the way the fold reads them.
// A torn line is skipped rather than repaired: it is the subject of its own test.
const documentsOf = (name: string): unknown[] => {
  const text = textOf(name);
  const lines = name.endsWith(".jsonl.gz") || name.endsWith(".corrupt.gz") ? text.split("\n") : [text];
  const docs: unknown[] = [];
  for (const line of lines) {
    if (line.trim() === "") continue;
    try {
      docs.push(JSON.parse(line));
    } catch {
      /* the torn write, and only that */
    }
  }
  return docs;
};

const disallowed = (key: string): boolean => {
  const type = key.slice(0, key.indexOf(":"));
  const value = key.slice(key.indexOf(":") + 1);
  if (UNCHECKED.has(type)) return false;
  const allowed = ALLOWED.get(type);
  return allowed === undefined || !allowed.test(value);
};

describe("the committed fixtures carry nothing real", () => {
  it("has the ledger, projection and folds for ten runs, plus the torn one", () => {
    expect(files.filter((name) => name.endsWith(".jsonl.gz"))).toHaveLength(10);
    expect(files.filter((name) => name.endsWith(".projection.json.gz"))).toHaveLength(10);
    expect(files.filter((name) => name.endsWith(".folds.json.gz"))).toHaveLength(10);
    expect(files.filter((name) => name.endsWith(".corrupt.gz"))).toHaveLength(1);
  });

  // Counted so the sweep below cannot pass by reading nothing: a fixture that
  // stopped being found is a fixture that stopped being checked.
  it("has the recorded run the replay gate reads", () => {
    expect(replayed.filter((name) => name.endsWith(".jsonl.gz"))).toHaveLength(1);
  });

  it.each(swept)("%s holds no identifier outside the allowlist", (name) => {
    const offenders = [...new Set(fromText(textOf(name)).map((found) => `${found.type}:${found.value}`))].filter(disallowed);

    expect(offenders).toEqual([]);
  });

  // fromText can only yield the types its own patterns describe, so a `user` --
  // which reaches the fold through the payload's key names -- is invisible to the
  // sweep above. This is the pass that sees it.
  //
  // Over the hunt documents only. pseudonyms.json is not one: it is a map, and
  // typeForKey reads its `counts` block -- {"arn": 1, "user": 7} -- as an arn and a
  // user rather than as the tallies they are. Its replacements are still held to
  // the allowlist, by the text sweeps either side of this one.
  it.each(files)("%s holds no payload-typed identifier outside the allowlist", (name) => {
    const found = documentsOf(name).flatMap((doc) => fromPayload(doc));
    const offenders = [...new Set(found.map((one) => `${one.type}:${one.value}`))].filter(disallowed);

    expect(offenders).toEqual([]);
  });

  it.each(swept)("%s holds no identifying value the extractor does not type", (name) => {
    const text = textOf(name);
    const offenders = SHAPES.flatMap(([what, shape, allowed]) =>
      [...new Set(text.match(shape) ?? [])].filter((match) => !allowed.test(match)).map((match) => `${what}: ${match}`),
    );

    expect(offenders).toEqual([]);
  });

  it.each(swept)("%s holds no encoded payload", (name) => {
    const blobs = [...new Set(textOf(name).match(ENCODED) ?? [])].filter((run) => /[a-z]/.test(run) && /[A-Z]/.test(run));

    expect(blobs).toEqual([]);
  });

  // The map is committed so a later run can be sanitised consistently. It is keyed
  // by digest for the same reason the fixtures are rewritten at all: a cleartext
  // map would restate every value removed here, in the same commit.
  it("states its pseudonyms without restating what they replaced", () => {
    const table = JSON.parse(readFileSync(join(RUNS, PSEUDONYMS), "utf8")) as {
      map: Record<string, { type: string; to: string }>;
    };

    expect(Object.keys(table.map).length).toBeGreaterThan(0);
    expect(Object.keys(table.map).filter((key) => !/^[0-9a-f]{16}$/.test(key))).toEqual([]);
  });
});
