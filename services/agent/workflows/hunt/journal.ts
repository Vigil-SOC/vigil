import { EVENT_SCHEMA_VERSION, type NewEvent, type RunKind, type RunPayload } from "../../contracts/events.js";
import type { State } from "../../core/seams.js";
import { fold, type HuntEvent, type HuntKinds, type Projection } from "./ledger.js";

export type { HuntEvent, HuntKinds, Projection } from "./ledger.js";
import type { DirectiveQueue } from "./ports.js";
import type { HuntState } from "./types.js";

// run_id and run_kind come from the journal, not from each call site: a caller
// naming the wrong run would write into someone else's ledger.
export type Body = Omit<NewEvent<HuntKinds>, "run_id" | "run_kind">;

// The controller's ledger over the State seam. append stays synchronous so the
// decision logic reads unchanged; flush is what makes an iteration durable.
export class Journal {
  private events: HuntEvent[] = [];
  private pending: Body[] = [];
  private view: Projection | null = null;

  private constructor(
    private readonly state: State<HuntKinds>,
    readonly queue: DirectiveQueue,
    readonly runId: string,
    private readonly runKind: RunKind,
  ) {}

  // runKind is the fallback, not the answer: the kind was settled when the run
  // opened, and it is on the first event. Trusting a caller's instead is how one
  // ledger ends up with events stamped two different ways -- the resume job says
  // what it was told, and only the ledger knows what the run actually is.
  static async open(
    state: State<HuntKinds>,
    queue: DirectiveQueue,
    runId: string,
    runKind: RunKind = "hunt",
  ): Promise<Journal> {
    const events = await state.read(runId);
    const journal = new Journal(state, queue, runId, events[0]?.run_kind ?? runKind);
    journal.events = events;
    return journal;
  }

  // The run event carries the domain-free RunPayload as well as the hunt's own
  // state: resume reads the spec off it without knowing which workflow wrote it.
  static async create(
    state: State<HuntKinds>,
    queue: DirectiveQueue,
    runId: string,
    hunt: HuntState,
    runKind: RunKind = "hunt",
    envelope: Partial<RunPayload> = {},
  ): Promise<Journal> {
    const journal = new Journal(state, queue, runId, runKind);
    journal.append({ kind: "run", payload: { ...envelope, hunt } } as unknown as Body);
    await journal.flush();
    return journal;
  }

  // Buffered so an iteration lands as one transaction. ts stays empty: only the
  // store stamps, and inventing one here would disagree with what it recorded.
  append(body: Body): HuntEvent {
    const event = {
      ...body,
      run_id: this.runId,
      run_kind: this.runKind,
      seq: this.events.length,
      ts: "",
      schema_version: EVENT_SCHEMA_VERSION,
    } as HuntEvent;
    this.pending.push(body);
    this.events.push(event);
    this.view = null;
    return event;
  }

  patch(target: string, id: string, fields: Record<string, unknown>): void {
    this.append({ kind: "patch", payload: { target, id, fields } } as unknown as Body);
  }

  // Read back rather than trusted: what the store recorded, with the seq and ts it
  // assigned, becomes this log, so nothing here can drift from the ledger.
  async flush(): Promise<void> {
    if (this.pending.length === 0) return;
    const batch = this.pending;
    this.pending = [];
    const owned = batch.map((body) => ({ ...body, run_id: this.runId, run_kind: this.runKind }) as NewEvent<HuntKinds>);
    await this.state.append(this.runId, owned);
    this.events = await this.state.read(this.runId);
    this.view = null;
  }

  // Picks up what other writers put on the ledger since the last read -- the stream
  // journals each call's spend straight to State, never through append -- without
  // making the buffered iteration durable. The pending tail keeps its place after
  // the stored events, as it will when flush lands it.
  async refresh(): Promise<void> {
    const stored = await this.state.read(this.runId);
    const tail = this.events.slice(this.events.length - this.pending.length);
    this.events = [...stored, ...tail.map((event, at) => ({ ...event, seq: stored.length + at }))];
    this.view = null;
  }

  get projection(): Projection {
    if (this.view === null) this.view = fold(this.events);
    return this.view;
  }

  get log(): readonly HuntEvent[] {
    return this.events;
  }
}
