// The DSN the integration tests connect on.
//
// Deliberately not DATABASE_URL: scripts/lib.sh exports every key in .env, so any
// shell that has run start.sh carries one naming the developer's own database, and
// these tests write runs, leases and directives into whatever they are given. The
// wrong database was the passing case -- it failed only when pointed somewhere safe.
//
// The suffix check is a seatbelt, not the mechanism. CI passes agent_test and
// scripts/agent_testdb.sh stands up vigil_test, so anything else is a mistake worth
// stopping the run for rather than writing through.
const FALLBACK = "postgres://vigil:vigil@localhost:55432/vigil_test";

const nameOf = (dsn: string): string => {
  try {
    return new URL(dsn).pathname.slice(1);
  } catch {
    return "";
  }
};

const resolve = (): string => {
  const dsn = process.env["AGENT_TEST_DATABASE_URL"];
  if (dsn === undefined || dsn === "") return FALLBACK;

  const name = nameOf(dsn);
  if (!name.endsWith("_test")) {
    throw new Error(
      `AGENT_TEST_DATABASE_URL names "${name === "" ? dsn : name}", which is not a test database. ` +
        "These tests write runs, leases and directives, so the name has to end in _test. " +
        "Run scripts/agent_testdb.sh for a throwaway one.",
    );
  }
  return dsn;
};

export const TEST_DSN = resolve();
