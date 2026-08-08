import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    // The Postgres tests share one database; parallel files would race on the ledger.
    fileParallelism: false,
  },
});
