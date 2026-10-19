import type { DirectiveQueue } from "./ports.js";
import type { Directive } from "./types.js";

// The queue without Postgres. It reproduces rather than approximates the table's
// guarantees: insertion order, idempotence on directive_id, and no delete on read.
export class InProcessDirectiveQueue implements DirectiveQueue {
  private readonly runs = new Map<string, Directive[]>();

  private queue(runId: string): Directive[] {
    const existing = this.runs.get(runId);
    if (existing !== undefined) return existing;
    const created: Directive[] = [];
    this.runs.set(runId, created);
    return created;
  }

  async enqueue(runId: string, directive: Directive): Promise<void> {
    const queue = this.queue(runId);
    if (queue.some((queued) => queued.directive_id === directive.directive_id)) return;
    queue.push(structuredClone(directive));
  }

  async pending(runId: string, journaled: readonly string[]): Promise<Directive[]> {
    const taken = new Set(journaled);
    return structuredClone(this.queue(runId).filter((directive) => !taken.has(directive.directive_id)));
  }
}
