// The image overrides zod to 4.6.7 (GHSA-mrhx-c7xc-m3w5). The baked connectors
// were written against zod 3, which still had z.string().ip() and
// ZodError.errors, and server-github turns schemas into JSON with
// zod-to-json-schema, which does not read a zod 4 schema. Patch those copies
// in place so the connectors keep their current calls.
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);

const ZOD_MARKER = "vigil:zod-string-ip";
const ZOD_PATCH = `
// ${ZOD_MARKER}
{
  const __ipProto = Object.getPrototypeOf(z.string());
  if (typeof __ipProto.ip !== "function") {
    const __v4 = z.string().ipv4();
    const __v6 = z.string().ipv6();
    __ipProto.ip = function ip(params) {
      const message = typeof params === "string" ? params : params?.message;
      const version = params && typeof params === "object" ? params.version : undefined;
      if (version === "v4") return this.ipv4(message);
      if (version === "v6") return this.ipv6(message);
      return this.refine(
        (value) => __v4.safeParse(value).success || __v6.safeParse(value).success,
        message ?? "Invalid IP address",
      );
    };
  }
  const __errProto = Object.getPrototypeOf(z.string().safeParse(0).error);
  if (!Object.prototype.hasOwnProperty.call(__errProto, "errors")) {
    Object.defineProperty(__errProto, "errors", {
      configurable: true,
      enumerable: false,
      get() {
        return this.issues;
      },
    });
  }
}
`;

const SCHEMA_MARKER = "vigil:zod-4-json-schema";
const SCHEMA_GUARD = `if (schema && typeof schema === "object" && schema._zod && typeof schema._zod === "object") {
        return toJSONSchema(schema);
    }
    `;

function scan(dir, name, found) {
  let names;
  try {
    names = readdirSync(dir);
  } catch {
    return;
  }
  for (const entry of names) {
    if (entry === ".bin") continue;
    const path = join(dir, entry);
    if (entry.startsWith("@")) {
      for (const scoped of readdirSync(path)) scan(join(path, scoped, "node_modules"), name, found);
      continue;
    }
    if (entry === name) {
      try {
        const pkg = JSON.parse(readFileSync(join(path, "package.json"), "utf8"));
        if (pkg.name === name) found.push(path);
      } catch {
        // A directory with this name that is not the package.
      }
    }
    scan(join(path, "node_modules"), name, found);
  }
}

function patchZod(copy) {
  const esmPath = join(copy, "index.js");
  const cjsPath = join(copy, "index.cjs");
  const esm = readFileSync(esmPath, "utf8");
  if (!esm.includes(ZOD_MARKER)) {
    if (!esm.includes("import * as z ")) throw new Error(`${esmPath} has no zod namespace to patch`);
    writeFileSync(esmPath, `${esm}\n${ZOD_PATCH}`);
  }
  const cjs = readFileSync(cjsPath, "utf8");
  if (!cjs.includes(ZOD_MARKER)) {
    const seal = "// seal-cjs-exports";
    const at = cjs.indexOf(seal);
    if (at === -1) throw new Error(`${cjsPath} has no ${seal} marker`);
    writeFileSync(cjsPath, `${cjs.slice(0, at)}${ZOD_PATCH}\n${cjs.slice(at)}`);
  }

  return import(pathToFileURL(esmPath).href);
}

function patchSchema(copy) {
  const esmPath = join(copy, "dist/esm/zodToJsonSchema.js");
  const cjsPath = join(copy, "dist/cjs/zodToJsonSchema.js");
  const esm = readFileSync(esmPath, "utf8");
  if (!esm.includes(SCHEMA_MARKER)) {
    const needle = "const zodToJsonSchema = (schema, options) => {\n    const refs = getRefs(options);";
    if (!esm.includes(needle)) throw new Error(`${esmPath} did not match the expected function`);
    const patched = esm.replace(
      needle,
      `import { toJSONSchema } from "zod";\n// ${SCHEMA_MARKER}\nconst zodToJsonSchema = (schema, options) => {\n    ${SCHEMA_GUARD}const refs = getRefs(options);`,
    );
    writeFileSync(esmPath, patched);
  }
  const cjs = readFileSync(cjsPath, "utf8");
  if (!cjs.includes(SCHEMA_MARKER)) {
    const needle = "const zodToJsonSchema = (schema, options) => {\n    const refs = (0, Refs_js_1.getRefs)(options);";
    if (!cjs.includes(needle)) throw new Error(`${cjsPath} did not match the expected function`);
    const patched = cjs.replace(
      needle,
      `const { toJSONSchema } = require("zod");\n// ${SCHEMA_MARKER}\nconst zodToJsonSchema = (schema, options) => {\n    ${SCHEMA_GUARD}const refs = (0, Refs_js_1.getRefs)(options);`,
    );
    writeFileSync(cjsPath, patched);
  }
}

const zodCopies = [];
scan(join(ROOT, "node_modules"), "zod", zodCopies);
if (zodCopies.length === 0) throw new Error("no zod install to patch");
for (const copy of zodCopies) {
  const esm = await patchZod(copy);
  const ip = esm.z.string().ip("bad ip");
  if (!ip.safeParse("1.2.3.4").success || !ip.safeParse("2001:db8::1").success || ip.safeParse("nope").success) {
    throw new Error(`zod ip shim failed in ${copy}`);
  }
  if (!Array.isArray(esm.z.string().safeParse(1).error.errors)) {
    throw new Error(`zod errors shim failed in ${copy}`);
  }
  const cjs = require(join(copy, "index.cjs"));
  if (typeof cjs.z.string().ip !== "function" || !Array.isArray(cjs.z.string().safeParse(1).error.errors)) {
    throw new Error(`zod cjs shim failed in ${copy}`);
  }
}

const schemaCopies = [];
scan(join(ROOT, "node_modules"), "zod-to-json-schema", schemaCopies);
if (schemaCopies.length === 0) throw new Error("no zod-to-json-schema install to patch");
for (const copy of schemaCopies) patchSchema(copy);

const { z } = await import(pathToFileURL(join(zodCopies[0], "index.js")).href);
const { zodToJsonSchema } = await import(pathToFileURL(join(schemaCopies[0], "dist/esm/zodToJsonSchema.js")).href);
const converted = zodToJsonSchema(z.object({ owner: z.string() }));
if (converted.type !== "object" || converted.properties?.owner?.type !== "string") {
  throw new Error("zod 4 schema did not convert to a JSON schema object");
}
const cjsConverted = require(join(schemaCopies[0], "dist/cjs/zodToJsonSchema.js")).zodToJsonSchema(
  require(join(zodCopies[0], "index.cjs")).z.object({ owner: require(join(zodCopies[0], "index.cjs")).z.string() }),
);
if (cjsConverted.type !== "object") throw new Error("zod 4 cjs schema did not convert");
