// Log retention for the desktop shell. Deliberately free of `electron` imports
// so it can be exercised from a plain node script.
import type { ChildProcess } from "child_process";
import * as fs from "fs";
import * as path from "path";

const KEEP = 5;

export interface CaptureResult {
  bytes: number;
  timedOut: boolean;
  error?: Error;
}

// name -> name.1 -> … -> name.<keep-1>, dropping the oldest, so `keep` files exist.
export function rotateLog(file: string, keep = KEEP): void {
  try {
    fs.rmSync(`${file}.${keep - 1}`, { force: true });
    for (let i = keep - 2; i >= 1; i--) {
      if (fs.existsSync(`${file}.${i}`)) fs.renameSync(`${file}.${i}`, `${file}.${i + 1}`);
    }
    if (fs.existsSync(file)) fs.renameSync(file, `${file}.1`);
  } catch {
    // retention must never break startup
  }
}

// Rotates once, then returns an appender that swallows every write error.
export function openAppLog(dir: string, name = "vigil-desktop.log"): (line: string) => void {
  const file = path.join(dir, name);
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch {
    return () => {};
  }
  rotateLog(file);
  return (line) => {
    try {
      fs.appendFileSync(file, `${new Date().toISOString()} ${line}\n`);
    } catch {
      // disk full / permissions
    }
  };
}

// Stream a child's stdout+stderr into `file`. If `timeoutMs` elapses the child
// is killed and what was captured so far is kept, with a trailing note.
export function captureToFile(
  spawnFn: (args: string[]) => ChildProcess,
  args: string[],
  file: string,
  timeoutMs?: number,
): Promise<CaptureResult> {
  return new Promise((resolve) => {
    const result: CaptureResult = { bytes: 0, timedOut: false };
    let proc: ChildProcess;
    let out: fs.WriteStream;
    try {
      out = fs.createWriteStream(file);
      proc = spawnFn(args);
    } catch (e) {
      return resolve({ ...result, error: e as Error });
    }
    out.on("error", () => {});

    let done = false;
    let timer: NodeJS.Timeout | undefined;
    const finish = (extra?: string) => {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      proc.stdout?.removeAllListeners("data");
      proc.stderr?.removeAllListeners("data");
      if (extra) out.write(extra);
      out.end(() => resolve(result));
    };

    const sink = (d: Buffer | string) => {
      result.bytes += d.length;
      out.write(d);
    };
    proc.stdout?.on("data", sink);
    proc.stderr?.on("data", sink);
    proc.on("close", () => finish());
    proc.on("error", (e) => {
      result.error = e;
      finish();
    });

    if (timeoutMs) {
      timer = setTimeout(() => {
        result.timedOut = true;
        if (proc.pid) {
          try {
            process.kill(proc.pid, "SIGKILL");
          } catch {
            // already gone
          }
        }
        // Don't wait for "close": a grandchild can keep the pipes open.
        finish(`\n[truncated: log capture stopped after ${timeoutMs} ms]\n`);
      }, timeoutMs);
    }
  });
}

const stamp = (d: Date) => d.toISOString().replace(/[-:]/g, "").replace(/\.\d+/, "");

// Save the stack's full container logs to <logsDir>/containers/vigil-<UTC>.log,
// keep the newest `keep`, and never throw. Returns the file written, if any.
export async function snapshotContainerLogs(
  spawnFn: (args: string[]) => ChildProcess,
  composeLogsArgs: string[],
  logsDir: string,
  timeoutMs = 5000,
  keep = KEEP,
  now = new Date(),
): Promise<string | undefined> {
  try {
    const dir = path.join(logsDir, "containers");
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `vigil-${stamp(now)}.log`);
    const r = await captureToFile(spawnFn, composeLogsArgs, file, timeoutMs);
    if (r.error || r.bytes === 0) {
      fs.rmSync(file, { force: true }); // nothing worth keeping
      return undefined;
    }
    const old = fs
      .readdirSync(dir)
      .filter((f) => /^vigil-.*\.log$/.test(f))
      .sort()
      .slice(0, -keep);
    for (const f of old) fs.rmSync(path.join(dir, f), { force: true });
    return file;
  } catch {
    return undefined;
  }
}
