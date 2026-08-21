import { userInfo } from "node:os";
import { newId } from "./ids.js";
import type { Journal } from "./journal.js";
import type { DirectiveQueue } from "./ports.js";
import { DIRECTIVE_KINDS, type BudgetGrant, type Directive, type DirectiveKind } from "./types.js";

// The controller's own voice in the directive stream. The drain tells a queued
// directive from a controller note by id, not by this; it is for the report.
export const CONTROLLER_ACTOR = "controller";

// The stub operator a DEV_MODE deployment attributes to, so the attribution path
// is exercised with auth off rather than skipped. Read here rather than in
export const DEV_ACTOR = "dev-admin";

// Who this process is, for attribution. A directive's actor is a person who steered
// the run, which is a different thing from the lease's owner, a process.
function actorName(): string {
  return process.env["VIGIL_ACTOR"] || userInfo().username;
}

// Only meaningful for a directive queued from this process — a console answer
// carries the operator its own auth resolved, and passes it explicitly.
export function directiveActor(): string {
  if (process.env["VIGIL_ACTOR"]) return actorName();
  return process.env["DEV_MODE"] === "true" ? DEV_ACTOR : actorName();
}

export class InvalidDirective extends Error {}

// What an extension buys, read out of the operator's own words: "+5 iterations",
// "$10 more", "5 iterations and $2.50", "30 more minutes". Parsed once at queue time so the ledger
export function parseGrant(text: string): BudgetGrant {
  const iterations = /(\d+(?:\.\d+)?)\s*(?:more\s+)?iterations?/i.exec(text);
  const dollars = /\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:usd|dollars?)/i.exec(text);
  const cost = dollars?.[1] ?? dollars?.[2];
  const minutes = /(\d+(?:\.\d+)?)\s*(?:more\s+)?(?:minutes?|mins?)/i.exec(text);

  return {
    iterations: Math.max(0, Math.floor(Number(iterations?.[1] ?? 0))),
    cost_usd: Math.max(0, Number(cost ?? 0)),
    wall_ms: Math.max(0, Math.floor(Number(minutes?.[1] ?? 0) * 60_000)),
  };
}

export function grantOf(directive: Directive): BudgetGrant {
  return directive.grant ?? parseGrant(directive.text);
}

// What a directive may carry beyond its text: which checkpoint it answers, which
// entity it suppresses, which lead it pins. Typed at queue time so the drain
export type DirectiveFields = Partial<
  Pick<Directive, "actor" | "checkpoint_id" | "entity_key" | "question_id" | "hypothesis_id" | "tenant" | "revoke" | "grant">
>;

// The envelope, checked where another process writes across. A workflow's own fields
// are not: naming a checkpoint that does not exist is the controller's question.
export function validateDirective(directive: Directive): void {
  if (typeof directive.directive_id !== "string" || directive.directive_id.length === 0) {
    throw new InvalidDirective("a directive with no id cannot be journaled or excluded from the next drain");
  }
  if (!(DIRECTIVE_KINDS as readonly string[]).includes(directive.kind)) {
    throw new InvalidDirective(`unknown directive kind ${String(directive.kind)}`);
  }
  if (typeof directive.actor !== "string" || directive.actor.length === 0) {
    throw new InvalidDirective("a directive with no actor leaves the ledger unable to say who steered the run");
  }
  if (typeof directive.text !== "string") {
    throw new InvalidDirective("a directive's text is what reaches the digest, so it must be a string");
  }
  if (directive.grant !== undefined) validateGrant(directive.grant);
}

// A grant is arithmetic on a ceiling, so a value that is not a finite number is
// refused here rather than added: `max_iterations + NaN` is NaN, and `used >= NaN`
// is always false, which is a hunt with no ceiling at all.
function validateGrant(grant: BudgetGrant): void {
  for (const arm of ["iterations", "cost_usd", "wall_ms"] as const) {
    const asked = grant[arm];
    if (typeof asked !== "number" || !Number.isFinite(asked) || asked < 0) {
      throw new InvalidDirective(`a grant's ${arm} must be a finite number of at least 0, not ${String(asked)}`);
    }
  }
}

// Queues an operator's directive. It throws rather than firing the write off
// unawaited: an enqueue that failed must reach the operator, not vanish.
export async function steer(
  queue: DirectiveQueue,
  runId: string,
  kind: DirectiveKind,
  text: string,
  fields: DirectiveFields = {},
): Promise<Directive> {
  const directive: Directive = {
    directive_id: newId("dir", 4),
    actor: directiveActor(),
    kind,
    text,
    created_at: new Date().toISOString(),
    origin: "inbox",
    ...(kind === "extend" ? { grant: parseGrant(text) } : {}),
    ...fields,
  };
  validateDirective(directive);
  await queue.enqueue(runId, directive);
  return directive;
}

// The controller journaling its own note, so a refusal or a clamped extension
// reaches the next digest through exactly the channel an operator's note uses.
export function journalNote(ledger: Journal, text: string): Directive {
  const directive: Directive = {
    directive_id: newId("dir", 4),
    actor: CONTROLLER_ACTOR,
    kind: "note",
    text,
    created_at: new Date().toISOString(),
    origin: "controller",
  };
  ledger.append({ kind: "directive", payload: directive });
  return directive;
}

// Everything the ledger already holds, whoever wrote it, so this asks one question:
// has this directive reached the record?
function journaled(ledger: Journal): string[] {
  return ledger.projection.directives.map((directive) => directive.directive_id);
}

// What the next drain would take, without taking it. The hard abort reads this
// between dispatch settlements: an operator who hit abort mid-iteration should
export async function peek(ledger: Journal): Promise<Directive[]> {
  return ledger.queue.pending(ledger.runId, journaled(ledger));
}

// Skips what the ledger recorded rather than deleting from the queue, so a drain
// interrupted halfway re-runs. Only the run holding the ledger drains.
export async function drain(ledger: Journal): Promise<Directive[]> {
  const taken: Directive[] = [];

  for (const directive of await peek(ledger)) {
    try {
      validateDirective(directive);
    } catch (error) {
      // Journaled under its own id as a controller note, so the attempt is on the
      // record and the refusal happens once rather than on every later drain.
      const said = typeof directive.text === "string" && directive.text.length > 0 ? ` — ${directive.text}` : "";
      ledger.append({
        kind: "directive",
        payload: {
          ...directive,
          kind: "note",
          origin: "controller",
          text: `refused a malformed ${String(directive.kind)} directive: ${(error as Error).message}${said}`,
        },
      });
      continue;
    }
    // Normalised on the way in, not at every read: an extend from a writer that does
    // not parse prose still lands as numbers, so a grant reads the same whoever asked.
    const journaling =
      directive.kind === "extend" && directive.grant === undefined
        ? { ...directive, grant: grantOf(directive) }
        : directive;

    ledger.append({ kind: "directive", payload: journaling });
    taken.push(journaling);
  }

  return taken;
}
