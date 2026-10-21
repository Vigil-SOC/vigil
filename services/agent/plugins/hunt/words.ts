import type { CallPayload } from "../../contracts/events.js";
import { nameInWords } from "../../core/call-view.js";
import { ENTITY_TYPES } from "./types.js";

// A kernel hunt's moves, in the words a person reads on the case page and in Watch a run.
// The agent's calls are the record; these say what each one did, never its JSON.

// The hunt's one agent: plugin.yaml's lead role, which makes every call a kernel hunt
// journals. Named so "who acted" has an answer wherever the console asks it.
export const HUNT_AGENT = "Hunt lead";

// The move each of the hunt's own tools makes. Every other tool is the deployment's,
// and a call to one is a question put to the estate.
const ACTION: Readonly<Record<string, string>> = {
  conclude_hunt: "CONCLUDE",
  handoff_ir: "HANDOFF_IR",
  ask_operator: "CHECKPOINT",
  abandon_hypothesis: "ABANDON",
  validate_hypothesis: "VALIDATE",
  propose_hypothesis: "PROPOSE",
  link_evidence: "LINK",
  record_evidence: "RECORD",
  expand: "EXPAND",
  open_question: "QUESTION",
  close_question: "QUESTION",
  hunt_status: "READ",
  load_skill: "READ",
};
const QUERY = "QUERY";

// A turn that makes several moves is named for its most consequential one.
const RANK = ["CONCLUDE", "HANDOFF_IR", "CHECKPOINT", "ABANDON", "VALIDATE", "PROPOSE", "LINK", "RECORD", "EXPAND", QUERY, "QUESTION", "READ"];

export function actionOf(tool: string): string {
  return ACTION[tool] ?? QUERY;
}

// The deployment's tools a hunt calls, by what they ask; the one map, which call views label by
// too. A tool not listed reads as its own name in words, with the value it was asked about.
const ASKED: Readonly<Record<string, string>> = {
  search_findings: "Searched alerts for",
  get_finding: "Read alert",
  telemetry_search: "Searched telemetry for",
  splunk_search: "Searched Splunk for",
  lookup_indicators: "Looked up in threat intel:",
  recall_entity: "Recalled what memory holds on",
};

// A tool as a label on a call ("Looked up in threat intel"): its words without what leads into a value.
export function toolWords(tool: string): string {
  return ASKED[tool]?.replace(/ (?:for|on)$|:$/, "") ?? nameInWords(tool);
}

// The few values that say what a call was about, whichever argument carries them.
const SUBJECT_KEYS = ["query", "values", "entity_keys", "finding_id", "indicator", "value", "entity", "sql", "q", "name"];

const SHOWN = 3;

export function parsedArgs(text: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(text);
    return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function strings(value: unknown): string[] {
  if (typeof value === "string") return value.trim() === "" ? [] : [value.trim()];
  return Array.isArray(value) ? value.flatMap((one) => (typeof one === "string" && one.trim() !== "" ? [one.trim()] : [])) : [];
}

// "a, b, c and 2 more"
export function listed(values: readonly string[], shown = SHOWN): string {
  const head = values.slice(0, shown).map((one) => (one.length > 80 ? `${one.slice(0, 77)}...` : one));
  const rest = values.length - head.length;
  if (rest > 0) return `${head.join(", ")} and ${rest} more`;
  if (head.length < 2) return head.join("");
  return `${head.slice(0, -1).join(", ")} and ${head.at(-1)}`;
}

// "user:svc_backup" is a recall key; a person reads the value, not its kind.
const ENTITY_KIND = new RegExp(`^(?:${ENTITY_TYPES.join("|")}|account|file):(?=\\S)`);

function subjects(args: Record<string, unknown>): string[] {
  for (const key of SUBJECT_KEYS) {
    const found = strings(args[key]);
    if (found.length > 0) return key === "entity_keys" ? found.map((one) => one.replace(ENTITY_KIND, "")) : found;
  }
  return [];
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function clip(text: string, max = 160): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 3)}...`;
}

// What one group of same-tool calls in a turn did, in words.
function groupWords(tool: string, calls: readonly CallPayload[]): string {
  const args = calls.map((call) => parsedArgs(call.arguments));
  const all = (key: string) => args.flatMap((one) => strings(one[key]));
  switch (tool) {
    case "record_evidence": {
      const results = args.flatMap((one) => (Array.isArray(one["results"]) ? one["results"] : []));
      const first = results.map((one) => (typeof one === "object" && one !== null ? (one as { summary?: unknown }).summary : null)).find((one) => typeof one === "string");
      const head = `Recorded ${plural(results.length, "piece")} of evidence`;
      return typeof first === "string" ? `${head}: ${clip(first)}` : head;
    }
    case "link_evidence": {
      const links = args.flatMap((one) => (Array.isArray(one["links"]) ? one["links"] : []));
      return `Weighed ${plural(links.length, "piece")} of evidence against the explanations`;
    }
    case "propose_hypothesis":
      return `Proposed an explanation: ${listed(all("statement").map((one) => clip(one)), 1)}`;
    case "validate_hypothesis":
      return `Put ${calls.length === 1 ? "an explanation" : plural(calls.length, "explanation")} up for a verdict`;
    case "abandon_hypothesis": {
      const reasons = all("reason");
      return `Set an explanation aside${reasons.length > 0 ? `: ${clip(reasons[0] as string)}` : ""}`;
    }
    case "handoff_ir":
      return "Handed a proven explanation to incident response";
    case "ask_operator": {
      const asked = all("question");
      return asked.length > 0 ? `Asked you: ${clip(asked[0] as string)}` : "Stopped to ask you";
    }
    case "conclude_hunt":
      return "Asked to conclude the hunt";
    case "expand":
      return `Opened ${plural(all("evidence_ids").length, "evidence row")} in full`;
    case "open_question": {
      const asked = all("question");
      return asked.length === 1 ? `Opened a lead: ${clip(asked[0] as string)}` : `Opened ${plural(asked.length, "lead")}`;
    }
    case "close_question":
      return `Closed ${plural(calls.length, "lead")}`;
    case "hunt_status":
      return "Read the board";
    case "load_skill":
      return `Read the ${listed(all("name"))} ${calls.length === 1 ? "playbook" : "playbooks"}`;
    default: {
      const asked = args.flatMap(subjects);
      const verb = ASKED[tool] ?? `Ran ${tool.replace(/[_-]+/g, " ")}${asked.length > 0 ? " on" : ""}`;
      return asked.length > 0 ? `${verb} ${listed(asked)}` : verb.replace(/ (for|on|:)$/, "");
    }
  }
}

// One turn's calls in a line: the move it is named for, then how much else it did.
export function turnWords(calls: readonly CallPayload[]): { action: string; words: string } {
  if (calls.length === 0) return { action: "READ", words: "Read the board" };
  const action = RANK.find((rank) => calls.some((call) => actionOf(call.tool) === rank)) ?? QUERY;
  const lead = calls.find((call) => actionOf(call.tool) === action) as CallPayload;
  const same = calls.filter((call) => call.tool === lead.tool);
  const others = calls.length - same.length;
  const failed = same.filter((call) => !call.ok).length;
  const words = groupWords(lead.tool, same);
  const tail = [failed > 0 ? (failed === same.length ? "refused" : `${failed} refused`) : null, others > 0 ? `and ${plural(others, "other call")}` : null].filter(Boolean);
  return { action, words: tail.length > 0 ? `${words} (${tail.join(", ")})` : words };
}
