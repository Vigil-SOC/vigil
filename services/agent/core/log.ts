export type Level = "debug" | "info" | "warn" | "error";

export type Fields = Record<string, unknown>;

export interface Logger {
  debug(msg: string, fields?: Fields): void;
  info(msg: string, fields?: Fields): void;
  warn(msg: string, fields?: Fields): void;
  error(msg: string, fields?: Fields): void;
}

const RANK: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };

function minimum(): number {
  const asked = (process.env["VIGIL_LOG_LEVEL"] ?? "").toLowerCase();
  return asked in RANK ? RANK[asked as Level] : RANK.info;
}

// One JSON object per line, warn and up on stderr. `msg` is a constant: anything that
// varies (run_id, status, the error) goes in fields so a line groups by its call site.
export function logger(name: string): Logger {
  const emit = (level: Level, msg: string, fields: Fields = {}): void => {
    if (RANK[level] < minimum()) return;
    // Reserved keys come first in the line and win, so a field can never overwrite them.
    const head = { ts: new Date().toISOString(), level, logger: name, msg };
    const line = JSON.stringify({ ...head, ...fields, ...head });
    (RANK[level] >= RANK.warn ? process.stderr : process.stdout).write(`${line}\n`);
  };
  return {
    debug: (msg, fields) => emit("debug", msg, fields),
    info: (msg, fields) => emit("info", msg, fields),
    warn: (msg, fields) => emit("warn", msg, fields),
    error: (msg, fields) => emit("error", msg, fields),
  };
}

export function errorFields(error: unknown): { error_type: string; error: string } {
  return error instanceof Error
    ? { error_type: error.constructor.name, error: error.message }
    : { error_type: typeof error, error: String(error) };
}
