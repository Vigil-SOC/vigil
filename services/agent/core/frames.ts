import { createHash } from "node:crypto";
import type { ContentPayload, FramePayload, NewEvent, RunKind } from "../contracts/events.js";
import type { SkillSpec } from "../contracts/plugin.js";
import { canonical } from "./context.js";
import type { Message } from "./provider.js";
import { contentHash, type FrameSeen } from "./stream.js";

// What each model call was shown, as hashes, the bytes once per run as content events,
// so a replay rebuilds a call's input from this ledger alone. Nothing is re-queried.

type Event = NewEvent<Record<never, never>>;

export class Framer {
  private constructor(
    private readonly runId: string,
    private readonly runKind: RunKind,
    private readonly skills: readonly { name: string; hash: string; body: string }[],
    private readonly known: Set<string>,
    private readonly counts: Map<string, number>,
    // Refs of the calls already in the transcript when the stream started, in
    // order. Null when the history is the caller's own messages, not the ledger's.
    private readonly opening: readonly string[] | null,
    private readonly added: string[] = [],
  ) {}

  static async open(
    state: { read(runId: string): Promise<readonly { kind: string; payload: unknown }[]> },
    runId: string,
    runKind: RunKind,
    skills: readonly SkillSpec[],
    opening: readonly string[] | null,
  ): Promise<Framer> {
    const known = new Set<string>();
    const counts = new Map<string, number>();
    for (const event of await state.read(runId)) {
      if (event.kind === "content") known.add((event.payload as ContentPayload).hash);
      if (event.kind === "frame") {
        const role = (event.payload as FramePayload).role;
        counts.set(role, (counts.get(role) ?? 0) + 1);
      }
    }
    const hashed = skills.map((skill) => ({ name: skill.name, hash: contentHash(skill.body), body: skill.body }));
    return new Framer(runId, runKind, hashed, known, counts, opening);
  }

  // A tool result this stream added to the transcript, by the ref it was journaled under.
  note(ref: string): void {
    this.added.push(ref);
  }

  // The content events this frame needs that the ledger lacks, then the frame.
  events(seen: FrameSeen): Event[] {
    const content: Event[] = [];
    const hash = (text: string): string => {
      const hashed = contentHash(text);
      if (!this.known.has(hashed)) {
        this.known.add(hashed);
        content.push(this.event("content", { hash: hashed, text }));
      }
      return hashed;
    };
    const call = (this.counts.get(seen.role) ?? 0) + 1;
    this.counts.set(seen.role, call);
    const history = this.opening === null && seen.history.length > 0 ? hash(stable(seen.history)) : null;
    const payload: FramePayload = {
      role: seen.role,
      call,
      model_id: seen.model_id,
      provider_type: seen.provider_type,
      sampling: seen.sampling,
      system: hash(seen.system),
      task: hash(seen.task),
      recall: hash(seen.recall),
      tools: seen.tools.map((tool) => ({ id: tool.id, hash: hash(stable(tool)) })),
      skills: this.skills.map((skill) => ({ name: skill.name, hash: hash(skill.body) })),
      transcript: [...(this.opening ?? []), ...this.added.slice(0, seen.results)],
      history,
      fold: {
        policy: seen.fold.policy === null ? null : { ...seen.fold.policy },
        folded: seen.fold.folded,
        ...(seen.fold.ceiling === undefined ? {} : { ceiling: seen.fold.ceiling }),
      },
      tail: seen.tail === null ? null : hash(seen.tail),
      emit: seen.emit === null ? null : hash(stable(seen.emit)),
      input: seen.input,
    };
    return [...content, this.event("frame", payload)];
  }

  private event(kind: "content" | "frame", payload: ContentPayload | FramePayload): Event {
    return { run_id: this.runId, run_kind: this.runKind, kind, payload } as unknown as Event;
  }
}

// The one serialisation a hashed object is stored and compared in.
export function stable(value: unknown): string {
  return JSON.stringify(canonical(value));
}

export function historyFrom(text: string): Message[] {
  return JSON.parse(text) as Message[];
}

// A child run's id, from its parent and purpose, so a resumed parent finds the child it
// started. Shaped as a version-5-style UUID because the ledger keys runs by uuid.
export function childRunId(parentRunId: string, key: string): string {
  const hex = createHash("sha256").update(`${parentRunId}\n${key}`).digest("hex");
  const variant = ((parseInt(hex[16] as string, 16) & 0x3) | 0x8).toString(16);
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}
