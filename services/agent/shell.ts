// The read-only shell over one case's view (#4199): the agent image's third command.
//
// Each POST /run is stateless. The backend sends a tar of the case view and one
// command; this process takes a slot from a small pool, unpacks the tar into a
// fresh directory as the slot's owner uid, runs `bash -c` as the slot's runner uid,
// then kills everything the runner left and deletes the directory. The two uids
// share a gid: the view is group-readable and only scratch/ is group-writable, so
// the command can read the view and write scratch/ and nothing else, and cannot
// chmod its way out because it owns none of it. No network, credentials or
// packages: the container is the boundary for those (network_mode: none).
//
// Runs as root with SETUID, SETGID and KILL only, as the MCP runner does.
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { chmodSync, existsSync, unlinkSync } from "node:fs";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { constants } from "node:os";
import { join } from "node:path";
import { errorFields, logger } from "./core/log.js";

const log = logger("agent.shell");

// By path: a bare name would be looked up on PATH, as root.
const SETPRIV = "/usr/bin/setpriv";
const BASH = "/bin/bash";
const env = process.env;
const num = (name: string, fallback: number): number => {
  const value = Number(env[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
};

const SOCKET = env["SHELL_SOCKET"] ?? "";
const PORT = num("SHELL_PORT", 6992);
const WORK = env["SHELL_WORK_DIR"] ?? "/work";
const POOL = num("SHELL_POOL", 8);
const UID_BASE = num("SHELL_UID_BASE", 61000);
// The group that may create in WORK (the compose tmpfs's gid). Owners hold it; runners do not.
const WORK_GID = num("SHELL_WORK_GID", 60999);
// Under the backend's capability timeout, which also pays for the render.
const TIMEOUT_MS = num("SHELL_TIMEOUT_MS", 30_000);
// Well under the run's result_cap (20_000 characters of JSON).
const STDOUT_CAP = num("SHELL_STDOUT_BYTES", 8_000);
const STDERR_CAP = num("SHELL_STDERR_BYTES", 2_000);
const MAX_BODY = num("SHELL_MAX_VIEW_BYTES", 64 * 1024 * 1024);
const COMMAND_MAX = 8_000;
// Per runner uid: processes, and the largest file it may write (1 KiB blocks).
// /work is one tmpfs for every slot, so one call cannot fill it for the rest.
const NPROC = num("SHELL_NPROC", 64);
const FILE_KB = num("SHELL_FILE_KB", 32 * 1024);

const free = Array.from({ length: POOL }, (_, slot) => slot);

interface Ran {
  code: number;
  stdout: Buffer;
  stderr: Buffer;
  truncated: boolean;
  timedOut: boolean;
}

interface As {
  uid: number;
  gid: number;
  // Supplementary groups; none unless named.
  groups?: number[];
  // The directory the script's environment points HOME and TMPDIR into.
  home?: string;
  input?: Buffer;
  timeoutMs?: number;
  // The script's $0 and $1. Passed by position, never interpolated into the script.
  args?: string[];
}

// After the command exits, how long its pipes may stay open for the last bytes. A
// background process still holding them is killed in cleanup, not waited for.
const DRAIN_MS = 200;

// One process under setpriv, with its own process group so a timeout kills it whole.
function run(script: string, as: As): Promise<Ran> {
  const home = as.home ?? WORK;
  const child = spawn(
    SETPRIV,
    [`--reuid=${as.uid}`, `--regid=${as.gid}`, ...(as.groups ? [`--groups=${as.groups.join(",")}`] : ["--clear-groups"]), "--inh-caps=-all", "--", BASH, "-c", script, ...(as.args ?? [])],
    {
      // Root holds no DAC_OVERRIDE, so it cannot enter a call's 0750 directory:
      // the script, already under its uid, changes into it.
      cwd: WORK,
      detached: true,
      stdio: ["pipe", "pipe", "pipe"],
      env: { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: join(home, "scratch"), TMPDIR: join(home, "scratch"), LANG: "C.UTF-8" },
    },
  );
  const out: Buffer[] = [];
  const err: Buffer[] = [];
  let outBytes = 0;
  let errBytes = 0;
  let truncated = false;
  // Past the cap the stream is still drained, so a chatty command never blocks on a full pipe.
  const take = (into: Buffer[], used: number, cap: number, chunk: Buffer): number => {
    if (used < cap) into.push(chunk.subarray(0, cap - used));
    if (used + chunk.length > cap) truncated = true;
    return used + chunk.length;
  };
  child.stdout.on("data", (chunk: Buffer) => (outBytes = take(out, outBytes, STDOUT_CAP, chunk)));
  child.stderr.on("data", (chunk: Buffer) => (errBytes = take(err, errBytes, STDERR_CAP, chunk)));
  child.stdin.on("error", () => undefined); // a command that never reads its stdin
  child.stdin.end(as.input ?? Buffer.alloc(0));

  let timedOut = false;
  const timer = as.timeoutMs
    ? setTimeout(() => {
        timedOut = true;
        try {
          process.kill(-child.pid!, "SIGKILL");
        } catch {
          // already gone
        }
      }, as.timeoutMs)
    : undefined;
  return new Promise((resolve, reject) => {
    let done = false;
    const finish = (code: number): void => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      child.stdout.destroy();
      child.stderr.destroy();
      resolve({ code, stdout: Buffer.concat(out), stderr: Buffer.concat(err), truncated, timedOut });
    };
    // Not a served answer: the command never ran.
    child.on("error", (error) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      reject(error);
    });
    child.on("exit", (code, signal) => {
      const status = code ?? 128 + (signal ? (constants.signals[signal] ?? 0) : 0);
      // close fires once the pipes have flushed; a leftover holding them open does not delay the answer.
      const drained = setTimeout(() => finish(status), DRAIN_MS);
      child.on("close", () => {
        clearTimeout(drained);
        finish(status);
      });
    });
  });
}

const OWNER = (slot: number) => ({ uid: UID_BASE + 2 * slot, gid: UID_BASE + 2 * slot, groups: [WORK_GID] });
const RUNNER = (slot: number) => ({ uid: UID_BASE + 2 * slot + 1, gid: UID_BASE + 2 * slot });

async function execute(view: Buffer, command: string): Promise<Record<string, unknown>> {
  const slot = free.shift();
  if (slot === undefined) throw new Busy();
  const dir = join(WORK, `${slot}-${randomBytes(8).toString("hex")}`);
  try {
    // Unpacked by the owner, never by root: a member naming ../ or an absolute
    // path cannot reach past what that uid may write. umask 027 leaves the view
    // group-readable and not group-writable; scratch/ alone is group-writable.
    const unpacked = await run(
      `umask 027 && mkdir -m 0750 "$0" && cd "$0" && tar -x --no-same-owner --no-same-permissions -f - && mkdir -m 0770 scratch`,
      { ...OWNER(slot), input: view, timeoutMs: 60_000, args: [dir] },
    );
    if (unpacked.code !== 0) throw new Error(`unpack failed: ${unpacked.stderr.toString().slice(0, 500)}`);
    // exec keeps the command in the process group a timeout kills.
    const ran = await run(`ulimit -u ${NPROC} -f ${FILE_KB} && cd -- "$0" && exec ${BASH} -c "$1"`, {
      ...RUNNER(slot),
      home: dir,
      timeoutMs: TIMEOUT_MS,
      args: [dir, command],
    });
    return {
      exit_code: ran.code,
      stdout: ran.stdout.toString("utf8"),
      stderr: ran.stderr.toString("utf8"),
      truncated: ran.truncated,
      timed_out: ran.timedOut,
      timeout_ms: TIMEOUT_MS,
    };
  } finally {
    // A slot whose runner still has a process is never handed out again: that
    // process could read or write the next call's.
    if (await cleanup(slot, dir)) free.push(slot);
    else log.error("shell slot quarantined: its runner could not be cleaned up", { slot });
  }
}

// kill -1 answers 0 even with nothing to signal, so whether the runner has a
// live process left is read from /proc, in bash alone so the scan never counts
// itself. Zombies are skipped: they run nothing and init reaps them.
const SWEEP =
  `alive() { local s k v u st; for s in /proc/[0-9]*/status; do [ "$s" = "/proc/$BASHPID/status" ] && continue; ` +
  `u=; st=; while read -r k v _; do case $k in State:) st=$v;; Uid:) u=$v; break;; esac; done < "$s" 2>/dev/null; ` +
  `[ "$u" = "$UID" ] && [ "$st" != Z ] && return 0; done; return 1; }; ` +
  `gone=; for ((i = 0; i < 50; i++)); do kill -KILL -1 2>/dev/null; alive || { gone=1; break; }; sleep 0.1; done; `;

// The runner's every process, process group or not, then its files, then the
// owner's directory. kill -1 from the runner's own uid reaches exactly its
// processes. True only when the runner is left with none.
async function cleanup(slot: number, dir: string): Promise<boolean> {
  try {
    const swept = await run(
      SWEEP +
        `chmod -R u+rwx "$0/scratch" 2>/dev/null; find "$0/scratch" -mindepth 1 -delete 2>/dev/null; ` +
        `find /dev/mqueue -mindepth 1 -user "$UID" -delete 2>/dev/null; [ -n "$gone" ]`,
      { ...RUNNER(slot), timeoutMs: 10_000, args: [dir] },
    );
    const removed = await run(`rm -rf "$0"`, { ...OWNER(slot), timeoutMs: 10_000, args: [dir] });
    return swept.code === 0 && !swept.timedOut && removed.code === 0;
  } catch (error) {
    log.error("shell cleanup failed", { slot, ...errorFields(error) });
    return false;
  }
}

class Busy extends Error {}

function reply(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

async function body(req: IncomingMessage): Promise<Buffer | null> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY) return null;
    chunks.push(chunk as Buffer);
  }
  return Buffer.concat(chunks);
}

const server = createServer((req, res) => {
  if (req.method === "GET" && (req.url === "/healthz" || req.url === "/readyz")) return reply(res, 200, { ok: true, free: free.length });
  if (req.method !== "POST" || req.url !== "/run") return reply(res, 404, { error: "not found" });
  void (async () => {
    // A client gone mid-upload must not take the process, and every call, with it.
    try {
      const raw = await body(req);
      if (raw === null) return reply(res, 413, { error: `view over ${MAX_BODY} bytes` });
      let parsed: { command?: unknown; view?: unknown };
      try {
        parsed = JSON.parse(raw.toString("utf8"));
      } catch {
        return reply(res, 400, { error: "body is not JSON" });
      }
      const { command, view } = parsed;
      if (typeof command !== "string" || !command.trim() || command.length > COMMAND_MAX || command.includes("\0")) {
        return reply(res, 400, { error: `command must be a non-empty string under ${COMMAND_MAX} characters, with no NUL` });
      }
      if (typeof view !== "string") return reply(res, 400, { error: "view must be a base64 tar" });
      reply(res, 200, await execute(Buffer.from(view, "base64"), command));
    } catch (error) {
      if (error instanceof Busy) return reply(res, 503, { error: "no shell slot is free" });
      log.error("shell call failed", errorFields(error));
      if (!res.headersSent && !res.destroyed) reply(res, 500, { error: error instanceof Error ? error.message : String(error) });
    }
  })();
});

if (process.getuid?.() !== 0) {
  log.error("the shell runs as root to drop each call to its own uid; refusing to start");
  process.exit(1);
}

if (SOCKET) {
  if (existsSync(SOCKET)) unlinkSync(SOCKET); // left by a previous container
  server.listen(SOCKET, () => {
    // The backend's uid connects; the directory is what keeps anyone else away.
    chmodSync(SOCKET, 0o666);
    log.info("shell listening", { socket: SOCKET, pool: POOL, timeout_ms: TIMEOUT_MS });
  });
} else {
  server.listen(PORT, env["AGENT_BIND_HOST"] || undefined, () => log.info("shell listening", { port: PORT, pool: POOL, timeout_ms: TIMEOUT_MS }));
}

for (const signal of ["SIGTERM", "SIGINT"] as const) process.on(signal, () => server.close(() => process.exit(0)));
