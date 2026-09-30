// The run's notebook, and the principle that a cause is proven only by what links
// it, enforced in code. Every finding answers when, who (the identity on the event),
// session (the credential behind it), what (the action) and why (the finding that
// caused or enabled it, or open). Who, session and evidence must be copied from rows
// the model has been shown. A why is proven only by its link: a value the cause's
// event produced and this event used (commit sha, image, function, key id, pod,
// request id), found in one row together with the cause's evidence and in one row
// together with this finding's evidence. An IP, a time or an identity is never a
// link: sharing one does not make one event the cause of another. A why not proven
// stays open; open whys ride along with the results, and finishing is refused while
// any is open, a bounded number of times.

// Where a check reaches the store. Undefined answers mean the store could not say,
// which leaves the finding open rather than passing it.
export interface Lookups {
  // Events carrying this exact value before the epoch second given.
  countBefore(value: string, before: number): Promise<number | undefined>;
  // Sourcetypes carrying the value anywhere.
  sourcesOf(value: string): Promise<string[]>;
  // Events mentioning the value, before the epoch second given or at all.
  mentions(value: string, before?: number): Promise<number | undefined>;
  // First event time of each sourcetype, epoch seconds.
  starts: ReadonlyMap<string, number>;
}

export interface Finding {
  id: string;
  what: string;
  who: string;
  session: string;
  when: string;
  evidence: string;
  why: string;
  link?: string;
}

export interface Hypothesis {
  id: string;
  text: string;
  test: string;
  status: string;
}

// A link must be something the cause produced: no event before the cause's own
// carries it. A repo, a node, a cluster or a config map existed before and fails.
const PRIOR_SLACK_S = 2;
// Where the logs begin, "nothing earlier" proves nothing.
const LOG_START_MARGIN_S = 600;
const CORRELATE_S = 3;
// An origin is an outsider seen a few times; an active insider is hundreds.
const MAX_ORIGIN_EVENTS = 10;
export const MAX_REFUSALS = 8;

// A why that starts like this is still open, whatever follows.
export const OPEN = /^\s*(\?|(unknown|tbd|open|not (yet )?(known|found)|unclear)\b)/i;
const ORIGIN = /^\s*origin\b/i;
// Fields naming an execution context (for taint links), and identity fields that never qualify.
const CONTEXT_FIELD = /(^|[._])(pod|pod[_-]?name|container|container[_-]?id|ocicontainerid|containerid|process[_-]?id|pid|ppid|targetprocessid|parentprocessid|contextprocessid|sessionprocessid|session[_-]?id|logon[_-]?id)(\{\})?$|pod-name/i;
const IDENTITY_FIELD = /(user|username|serviceaccount|principal|role|arn|actor|account)/i;
const IP = /^\s*\d{1,3}(\.\d{1,3}){3}\s*$|^[0-9a-f:]+:[0-9a-f:]+$/i;
const TIME = /^\s*\d{4}-\d\d-\d\d([T ]\d\d:\d\d(:\d\d)?)?/;
// Everyone who uses a role, a service account or a mailbox shares it, so it links
// two events no better than an IP does.
const IDENTITY = /arn:aws:(iam|sts)::\d*:(role|user|group|root|assumed-role|federated-user)\b|(^|[\s/])assumed-role\/|^system:(serviceaccount|node|anonymous)|^[^@\s]+@[^@\s]+\.[a-z]{2,}$/i;
// A sentence that clears someone may name them; "suspected" still points at them.
const HEDGE = /\b(legitimate|benign|ruled out|not (the |an )?(attacker|malicious|involved)|normal (work|activity|operations)|expected activity)\b/i;
const BENIGN = /\b(false[- ]positive|benign|not malicious|no (real )?incident|legitimate (automation|activity)|expected behaviou?r)\b/i;
const LINK_HINT =
  "A link is a value both events carry: a process id, a connection (host:port → IP), a file name or hash, a job/run/request id, an object or function name.";

// Rows print as JSON, so a copied value may carry escaped quotes and backslashes.
export const canon = (value: string): string =>
  value.replace(/\\"/g, '"').replace(/\\+/g, "\\").replace(/^["'`]+|["'`]+$/g, "").trim().toLowerCase();

const refOf = (finding: Finding): string | undefined => finding.why.match(/^\s*(F\d+)\b/)?.[1];
const epoch = (when: string): number => Date.parse(when) / 1000;

function flatten(value: unknown, key = "", out: [string, string][] = []): [string, string][] {
  if (Array.isArray(value)) value.forEach((one) => flatten(one, key, out));
  else if (value !== null && typeof value === "object") {
    for (const [field, held] of Object.entries(value)) flatten(held, key ? `${key}.${field}` : field, out);
  } else if (value !== null && value !== undefined) out.push([key, String(value)]);
  return out;
}

export interface NotebookState {
  list: Finding[];
  hyps: Hypothesis[];
  refusals: number;
  dispositionAsked: boolean;
}

export class Findings {
  list: Finding[] = [];
  hyps: Hypothesis[] = [];
  refusals = 0;
  private dispositionAsked = false;
  private readonly seen: string[] = [];
  // Every result row as shown, one JSON line each, for the row checks.
  private readonly rows: string[] = [];
  private readonly counts = new Map<string, number>();
  private readonly linkSources = new Map<string, string[]>();
  private readonly before = new Map<string, number>();
  private readonly total = new Map<string, number>();
  private readonly beforeKey = (finding: Finding) => `${canon(finding.who)}|${finding.when}`;

  constructor(private readonly lookups: Lookups) {}

  // What the model was shown. Only these rows count as evidence it can cite.
  saw(rows: readonly string[]): void {
    for (const row of rows) {
      this.rows.push(row);
      this.seen.push(canon(row));
    }
  }

  snapshot(): NotebookState {
    return { list: this.list, hyps: this.hyps, refusals: this.refusals, dispositionAsked: this.dispositionAsked };
  }

  // A resumed run's notebook, with the provenance counts asked again: they are the
  // store's answer, not the notebook's, and a resume asks the store.
  async restore(state: NotebookState): Promise<void> {
    this.list = state.list;
    this.hyps = state.hyps;
    this.refusals = state.refusals;
    this.dispositionAsked = state.dispositionAsked;
    await this.provenance();
    for (const finding of this.list) if (ORIGIN.test(finding.why)) await this.origin(finding);
  }

  private inResults(value: string): boolean {
    const wanted = canon(value);
    return wanted.length > 0 && this.seen.some((row) => row.includes(wanted));
  }

  private together(a: string, b: string): boolean {
    const x = canon(a);
    const y = canon(b);
    return x !== "" && y !== "" && this.seen.some((row) => row.includes(x) && row.includes(y));
  }

  // The row an evidence value was read from, flattened to field paths.
  private rowOf(evidence: string): Record<string, string> | undefined {
    const wanted = canon(evidence);
    for (const line of this.rows) {
      if (!canon(line).includes(wanted)) continue;
      try {
        const out: Record<string, string> = {};
        for (const [key, value] of flatten(JSON.parse(line))) out[key] = out[key] ? `${out[key]} ${value}` : value;
        return out;
      } catch {
        continue;
      }
    }
    return undefined;
  }

  correlated = (finding: Finding): boolean => /^\s*correlated\b/i.test(finding.link ?? "") && !this.unproven(finding);

  // A host-side network event and the cloud call it made, seconds apart. Weaker than
  // a shared value, and labelled so.
  private correlation(finding: Finding, cause: Finding): string | undefined {
    const apart = Math.abs(Date.parse(finding.when) - Date.parse(cause.when)) / 1000;
    if (Number.isNaN(apart) || apart > CORRELATE_S) {
      return `correlated needs the two events within ${CORRELATE_S} s; these are ${Number.isNaN(apart) ? "?" : Math.round(apart)} s apart`;
    }
    const a = this.rowOf(finding.evidence);
    const b = this.rowOf(cause.evidence);
    if (a === undefined || b === undefined) return "correlated: cannot find both events' rows in the results you have seen";
    const net = (row: Record<string, string>) =>
      Object.entries(row).filter(([key]) => /domain|host|remote|dest|query|url|sni|server/i.test(key)).map(([, value]) => value.toLowerCase()).join(" ");
    const service = (row: Record<string, string>) => (row["eventSource"] ?? "").toLowerCase().split(".")[0] ?? "";
    const names = (x: Record<string, string>, y: Record<string, string>) => {
      const called = service(y);
      return called !== "" && net(x).includes(called);
    };
    if (!names(a, b) && !names(b, a)) {
      return "correlated: one event must be a network event whose destination names the cloud service the other event called (its eventSource)";
    }
    return undefined;
  }

  // Taint: once the cause shows the attacker inside a pod, container, process or
  // session, a later event from that same context is caused by it. The value must sit
  // in a context field, never a user, role, host or repo, in both rows.
  private contextField(link: string, evidence: string): string | undefined {
    const l = canon(link);
    const e = canon(evidence);
    for (const line of this.rows) {
      const c = canon(line);
      if (!c.includes(l) || !c.includes(e)) continue;
      let row: unknown;
      try {
        row = JSON.parse(line);
      } catch {
        continue;
      }
      const hit = flatten(row).find(
        ([key, value]) => CONTEXT_FIELD.test(key) && !IDENTITY_FIELD.test(key.split(/[./]/).pop() ?? key) && canon(value).includes(l),
      );
      if (hit !== undefined) return hit[0];
    }
    return undefined;
  }

  // Why a finding's why is not proven, or undefined when it is.
  unproven(finding: Finding): string | undefined {
    const ref = refOf(finding);
    if (ref === undefined && ORIGIN.test(finding.why)) {
      const earlier = this.before.get(this.beforeKey(finding));
      const all = this.total.get(canon(finding.who));
      if (earlier === undefined || all === undefined) return "origin not checked yet";
      if (earlier > 0) return `not an origin: "${finding.who}" is in ${earlier.toLocaleString("en-US")} earlier events. Find what came before`;
      if (all > MAX_ORIGIN_EVENTS) {
        return (
          `not an origin: "${finding.who}" is an active account (${all.toLocaleString("en-US")} events); the logs starting with it does not ` +
          "make it the start of the attack. Find what used or compromised it"
        );
      }
      return undefined;
    }
    if (ref === undefined) {
      return OPEN.test(finding.why)
        ? "why unknown: find the finding that caused it, or write why = origin if nothing in the logs comes before it (code checks that its who appears in no earlier event)"
        : "no why";
    }
    const cause = this.list.find((one) => one.id === ref);
    if (cause === undefined) return `${ref} not recorded yet`;
    const link = finding.link?.trim() ?? "";
    if (link === "") return `no link to ${ref}: give the value ${ref}'s event produced that this event used. ${LINK_HINT}`;
    if (/^\s*correlated\b/i.test(link)) return this.correlation(finding, cause);
    if (IP.test(link) || TIME.test(link)) return `link "${link}" is an IP or a time, which proves nothing: give what ${ref}'s event produced that this event used`;
    if (IDENTITY.test(link) || this.list.some((one) => canon(one.who) === canon(link))) {
      return (
        `link "${link}" is an identity (a role, user, session, service account or someone's name): sharing one does not make one act ` +
        `the cause of the other. Give the artifact: what ${ref}'s event produced that this event used`
      );
    }
    const sources = this.linkSources.get(canon(link)) ?? [];
    const young = sources.filter((source) => (this.lookups.starts.get(source) ?? 0) > epoch(cause.when) - LOG_START_MARGIN_S);
    if (young.length > 0) {
      return (
        `link "${link}" first appears where the ${young.join(", ")} logs begin, so the logs cannot show ${ref} created it. ` +
        "Give a link from later in the logs, or find the event that produced it"
      );
    }
    const prior = this.counts.get(`${canon(link)}|${cause.when}`);
    const tc = Date.parse(cause.when);
    const te = Date.parse(finding.when);
    const taint =
      prior !== undefined && prior > 0 && !Number.isNaN(tc) && !Number.isNaN(te) && te >= tc - 1000 &&
      this.contextField(link, cause.evidence) !== undefined && this.contextField(link, finding.evidence) !== undefined;
    if (taint) return undefined;
    if (prior !== undefined && prior > 0) {
      return (
        `link "${link}" is in ${prior.toLocaleString("en-US")} events before ${ref}'s event, so ${ref} did not produce it: it is something ` +
        `that already existed (a repo, node, cluster, account). Give what ${ref}'s event created that this event used (commit sha, ` +
        "PR/run/job id, image digest, object or function ARN, key id, file, process id). Or, if " +
        `${ref} shows the attacker running code inside a pod, container, process or session and this event came later from that same ` +
        "context, give that context id as the link: it must sit in a pod/container/process/session field in both events' rows"
      );
    }
    if (!this.together(link, cause.evidence)) {
      return `link "${link}" is not in any row with ${ref}'s evidence "${cause.evidence.slice(0, 60)}": show that ${ref}'s event carries it. ${LINK_HINT}`;
    }
    if (!this.together(link, finding.evidence)) {
      return `link "${link}" is not in any row with this finding's evidence "${finding.evidence.slice(0, 60)}": show that this event carries it. ${LINK_HINT}`;
    }
    return undefined;
  }

  isOpen = (finding: Finding): boolean => this.unproven(finding) !== undefined;
  open = (): Finding[] => this.list.filter(this.isOpen);

  // The tool call: read the notebook, record a hypothesis, or write one finding.
  async call(input: Record<string, unknown>): Promise<string> {
    const op = input["op"];
    if (op === "read") return this.table();
    if (op === "hypothesis") return this.hypothesis(input);
    if (op !== "write") return "refused: op must be read, write or hypothesis";

    const fields = Object.fromEntries(
      (["id", "what", "who", "session", "when", "evidence", "why", "link"] as const)
        .filter((key) => typeof input[key] === "string")
        .map((key) => [key, input[key] as string]),
    ) as Partial<Finding>;
    const out = this.add(fields);
    if (out.startsWith("refused")) return out;

    await this.provenance();
    const written = this.list.find((one) => one.id === (fields.id ?? this.list[this.list.length - 1]?.id));
    if (written === undefined) return out;
    if (ORIGIN.test(written.why)) {
      await this.origin(written);
      const why = this.unproven(written);
      return `recorded ${written.id}${why ? ` — why is OPEN (${why})` : " — origin confirmed: nothing in the logs mentions it earlier"}.\n${this.line()}`;
    }
    const why = this.unproven(written);
    return `recorded ${written.id}${why ? ` — why is OPEN (${why})` : ` — why proven: ${refOf(written)} → ${written.id} via ${written.link}`}.\n${this.line()}`;
  }

  private hypothesis(input: Record<string, unknown>): string {
    const id = typeof input["id"] === "string" ? input["id"] : undefined;
    const text = typeof input["text"] === "string" ? input["text"] : undefined;
    const test = typeof input["test"] === "string" ? input["test"] : undefined;
    const status = typeof input["status"] === "string" ? input["status"] : undefined;
    const prev = id === undefined ? undefined : this.hyps.find((one) => one.id === id);
    if (prev !== undefined) {
      if (text) prev.text = text;
      if (test) prev.test = test;
      if (status) prev.status = status;
    } else {
      if (!text?.trim()) return "refused: a hypothesis needs text (what you think happened)";
      this.hyps.push({ id: `H${this.hyps.length + 1}`, text, test: test ?? "", status: status ?? "open" });
    }
    const held = prev ?? this.hyps[this.hyps.length - 1]!;
    return `hypothesis ${held.id} (${held.status}) recorded. Test it, then write what the events show as findings.`;
  }

  // Provenance of every link that now has both ends: events carrying it before its
  // cause's event, and which sources carry it at all.
  private async provenance(): Promise<void> {
    for (const finding of this.list) {
      const cause = this.list.find((one) => one.id === refOf(finding));
      const link = finding.link?.trim();
      const at = cause === undefined ? NaN : epoch(cause.when);
      if (cause === undefined || !link || Number.isNaN(at) || /^\s*correlated\b/i.test(link)) continue;
      const key = `${canon(link)}|${cause.when}`;
      if (!this.counts.has(key)) {
        const prior = await this.lookups.countBefore(link, at - PRIOR_SLACK_S);
        if (prior !== undefined) this.counts.set(key, prior);
      }
      if (!this.linkSources.has(canon(link))) this.linkSources.set(canon(link), await this.lookups.sourcesOf(link));
    }
  }

  // An origin claim: the events that mention its who before its time, and at all.
  private async origin(finding: Finding): Promise<void> {
    const at = epoch(finding.when);
    if (!this.before.has(this.beforeKey(finding)) && !Number.isNaN(at)) {
      const earlier = await this.lookups.mentions(finding.who, at - 1);
      if (earlier !== undefined) this.before.set(this.beforeKey(finding), earlier);
    }
    if (!this.total.has(canon(finding.who))) {
      const all = await this.lookups.mentions(finding.who);
      if (all !== undefined) this.total.set(canon(finding.who), all);
    }
  }

  add(input: Partial<Finding>): string {
    // An id the model picks for a new finding is kept if free; any other unknown id gets the next number.
    const prev = input.id === undefined ? undefined : this.list.find((one) => one.id === input.id);
    const fields = input.id !== undefined && prev === undefined && !/^F\d+$/.test(input.id) ? { ...input, id: undefined } : input;
    // A recorded finding is one event: an update may change its why, link and
    // session, never which event it is, or an unprovable fact could be overwritten.
    if (prev !== undefined) {
      const moved = (["when", "who", "what", "evidence"] as const).filter(
        (key) => fields[key] !== undefined && canon(String(fields[key])) !== canon(prev[key]),
      );
      if (moved.length > 0) {
        return `refused: ${prev.id} is fixed as recorded (${moved.join(", ")} cannot change). An update may change only why, link and session. For a different event, write a new finding.`;
      }
    }
    const finding = { ...prev, ...Object.fromEntries(Object.entries(fields).filter(([, value]) => value !== undefined)) } as Finding;
    if (!finding.id) finding.id = `F${this.list.length + 1}`;
    while (prev === undefined && this.list.some((one) => one.id === finding.id)) finding.id = `F${Number(finding.id.slice(1)) + 1}`;
    const missing = (["what", "who", "session", "when", "evidence", "why"] as const).filter((key) => !finding[key]?.trim());
    if (missing.length > 0) return `refused: missing ${missing.join(", ")}. Every finding answers what, who and why.`;

    const problems: string[] = [];
    if (!this.inResults(finding.who)) problems.push(`who "${finding.who}" is not in any result you have seen: copy the identity exactly from the event`);
    if (!/^\s*none\s*$/i.test(finding.session) && !this.inResults(finding.session)) {
      problems.push(`session "${finding.session}" is not in any result you have seen: copy it exactly from the event, or say none`);
    }
    if (!this.inResults(finding.evidence)) problems.push(`evidence "${finding.evidence}" is not in any result you have seen: copy a value exactly from the event row`);
    const ref = refOf(finding);
    if (ref === undefined && !OPEN.test(finding.why) && !ORIGIN.test(finding.why)) {
      problems.push(
        `why "${finding.why.slice(0, 80)}" is not a finding: give the id of the recorded finding that caused or enabled this (find and ` +
          "record it first), 'origin' if nothing in the logs comes before it, or 'unknown'",
      );
    }
    if (ref !== undefined && ref === finding.id) problems.push(`why points to ${finding.id} itself`);
    // No loops: following the whys back from ref must not come round to this finding.
    for (let at = ref, hops = 0; at !== undefined && hops < 100; hops += 1) {
      if (at === finding.id) {
        problems.push(`why ${ref} leads back to ${finding.id}: a cause must come before its effect`);
        break;
      }
      const next = this.list.find((one) => one.id === at);
      at = next === undefined ? undefined : refOf(next);
    }
    if (problems.length > 0) return `refused: ${problems.join("; ")}`;

    if (prev !== undefined) Object.assign(prev, finding);
    else this.list.push(finding);
    return `recorded ${finding.id}`;
  }

  // The alert is the latest finding; its proven whys walked back.
  provenIds(): Set<string> {
    const proven = new Set<string>();
    const alert = [...this.list].sort((a, b) => Date.parse(b.when) - Date.parse(a.when))[0];
    for (let at: Finding | undefined = alert; at !== undefined && !proven.has(at.id); ) {
      proven.add(at.id);
      if (this.unproven(at) !== undefined) break;
      const ref = refOf(at);
      at = this.list.find((one) => one.id === ref);
    }
    return proven;
  }

  // Actors the report names that the alert's proven chain does not reach: the who,
  // and an IP an action came FROM. Facts from an unlinked finding may be reported;
  // naming its actor as the attacker may not.
  unprovenNames(report: string): string[] {
    const proven = this.provenIds();
    const text = canon(report.split(/(?<=[.!?\n])\s+/).filter((sentence) => !HEDGE.test(sentence)).join(" "));
    const names = new Set<string>();
    for (const finding of this.list) {
      if (proven.has(finding.id)) continue;
      const short = finding.who.split(/[/:\\]/).filter(Boolean).pop() ?? finding.who;
      const from = [...finding.what.matchAll(/\bfrom\s+(?:ip\s+)?(\d{1,3}(?:\.\d{1,3}){3})\b/gi)].map((match) => match[1]!);
      for (const name of [finding.who, short, ...from]) {
        const wanted = canon(name);
        if (wanted.length <= 2 || !text.includes(wanted)) continue;
        if (this.list.some((one) => proven.has(one.id) && canon(one.who).includes(wanted))) continue;
        names.add(name);
      }
    }
    return [...names];
  }

  // Any actor the proven chain does not reach is replaced, outside sentences that
  // clear them. Applied to every report the run delivers.
  redact(report: string): string {
    const names = this.unprovenNames(report).sort((a, b) => b.length - a.length);
    if (names.length === 0) return report;
    const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return report
      .split(/(?<=[.!?\n])(?=\s)/)
      .map((sentence) => (HEDGE.test(sentence) ? sentence : names.reduce((text, name) => text.replace(new RegExp(escape(name), "gi"), "[unlinked actor]"), sentence)))
      .join("");
  }

  // Undefined accepts the report; otherwise the reason it goes back. Unlinked is not
  // unrelated, so a benign verdict over unlinked findings goes back once whatever the
  // budget. The rest only while budget is left, and at most MAX_REFUSALS times.
  gate(report: string, budgetLeft: boolean): string | undefined {
    const unlinked = this.list.length - this.provenIds().size;
    if (!this.dispositionAsked && unlinked > 0 && BENIGN.test(report.slice(0, 1500))) {
      this.dispositionAsked = true;
      return (
        `refused once: the report calls the alert benign or a false positive while ${unlinked} finding${unlinked > 1 ? "s you recorded are" : " you recorded is"} ` +
        "not linked to it. A link you could not prove is unknown, not absent: state the connection as unproven, and call the alert benign only if " +
        "events show it is normal activity, not because you could not link it."
      );
    }
    if (!budgetLeft || this.refusals >= MAX_REFUSALS) return undefined;
    const open = this.open();
    const named = this.unprovenNames(report);
    if (named.length > 0) {
      this.refusals += 1;
      return (
        `refused: the report names ${named.map((name) => `"${name}"`).join(", ")}, from findings the alert's proven chain does not reach. Name someone ` +
        "as the attacker only through proven links; otherwise say they are unverified or leave them out. Facts you observed (destinations, hosts, " +
        `files) can stay, marked as not linked.${open.length ? ` Open: ${open.map((one) => `${one.id} (${this.unproven(one)})`).join("; ")}.` : ""}`
      );
    }
    if (this.list.length === 0) {
      this.refusals += 1;
      return "refused: no findings recorded. Write what you found to the findings file (when, who, what, why), then finish.";
    }
    if (open.length === 0) return undefined;
    this.refusals += 1;
    return (
      `refused: ${open.length} why${open.length > 1 ? "s" : ""} still open: ` +
      open.map((one) => `${one.id} — why did ${one.who} ${one.what.slice(0, 80)} at ${one.when}? (${this.unproven(one)})`).join(" ") +
      " Find what caused or enabled each and the value that links them, update the finding, then finish."
    );
  }

  line(): string {
    const open = this.open();
    const detail = open.map((one) => `${one.id} (${one.when} · ${one.who} · ${one.what.slice(0, 60)} — ${this.unproven(one)})`).join("; ");
    return `FINDINGS ${this.list.length}, open whys ${open.length}${open.length ? `: ${detail}` : ""}`;
  }

  // The notebook as the model reads it: one row per finding, oldest first.
  table(): string {
    const hyps = this.hyps.length
      ? `\n\nHypotheses:\n${this.hyps.map((one) => `- ${one.id} (${one.status}): ${one.text}${one.test ? ` — test: ${one.test}` : ""}`).join("\n")}`
      : "";
    if (this.list.length === 0) return `findings file is empty${hyps}`;
    const cell = (value: string) => value.replace(/\|/g, "\\|").replace(/\n/g, " ");
    const mark = (finding: Finding) => (this.isOpen(finding) ? " (open)" : this.correlated(finding) ? " (correlated)" : "");
    return (
      [
        this.line(),
        "",
        "| id | when | who | session | what | why | link | evidence |",
        "|---|---|---|---|---|---|---|---|",
        ...this.list.map(
          (one) =>
            `| ${one.id}${mark(one)} | ${cell(one.when)} | ${cell(one.who)} | ${cell(one.session)} | ${cell(one.what)} | ${cell(one.why)} | ${cell(one.link ?? "")} | ${cell(one.evidence)} |`,
        ),
      ].join("\n") + hyps
    );
  }
}
