import { startWorker } from "../../worker.js";

// Drains one job and exits, so an integration test can spawn the worker without
// owning a long-lived process. Not a deployment entrypoint; that is worker.ts.
const DRAIN_TIMEOUT_MS = 30_000;

const worker = startWorker();
const stop = async (code: number) => {
  await worker.close();
  process.exit(code);
};

worker.on("completed", (job) => {
  console.log(`completed ${job.id}`);
  void stop(0);
});
worker.on("failed", (job, error) => {
  console.error(`failed ${job?.id}: ${error.message}`);
  void stop(1);
});

setTimeout(() => {
  console.error("no job consumed before the drain timeout");
  void stop(2);
}, DRAIN_TIMEOUT_MS);
