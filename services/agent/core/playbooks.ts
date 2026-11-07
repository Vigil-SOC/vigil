import { SpecError } from "./spec.js";

// A reference the worker resolves rather than a path it opens. Python holds the
// definitions, so a job names one and this side asks for it at run start.
export const WORKFLOW_SCHEME = "workflow:";

export interface PlaybookLayers {
  playbook: string;
  config: string;
}

export type PlaybookResolver = (reference: string) => Promise<PlaybookLayers>;

export interface ResolverOptions {
  url: string;
  token: string;
  fetch?: typeof globalThis.fetch;
}

export function isReference(playbook: string): boolean {
  return playbook.startsWith(WORKFLOW_SCHEME);
}

// The workflow a playbook names, or undefined for a path or a bare reference.
export function workflowIdOf(playbook: string): string | undefined {
  return isReference(playbook) ? playbook.slice(WORKFLOW_SCHEME.length).trim() || undefined : undefined;
}

function idOf(reference: string): string {
  const id = reference.slice(WORKFLOW_SCHEME.length).trim();
  if (id === "") throw new SpecError(`${reference} names no workflow`);
  return id;
}

// Text, not parsed layers: the endpoint answers with the two documents so this
// side reads them with parsePlaybook and parseConfig, the readers a file gets.
function layersOf(body: unknown, reference: string): PlaybookLayers {
  const value = body as Record<string, unknown> | null;
  if (typeof value !== "object" || value === null) throw new SpecError(`${reference} resolved to no document`);
  const { playbook, config } = value;
  if (typeof playbook !== "string" || typeof config !== "string") {
    throw new SpecError(`${reference} resolved without both a playbook and a config`);
  }
  return { playbook, config };
}

export function httpPlaybooks(options: ResolverOptions): PlaybookResolver {
  const call = options.fetch ?? globalThis.fetch;
  return async (reference: string): Promise<PlaybookLayers> => {
    const url = `${options.url.replace(/\/$/, "")}/${encodeURIComponent(idOf(reference))}`;
    let response: Response;
    try {
      response = await call(url, { headers: { authorization: `Bearer ${options.token}` } });
    } catch (error) {
      // Not a SpecError: the spec is fine and the endpoint is unreachable, which is
      // what a rolling upgrade looks like from here. The worker retires a SpecError
      // on the first attempt, so calling this one would spend the retry policy on
      // exactly the failure it was added for.
      throw new Error(`could not resolve ${reference}: ${error instanceof Error ? error.message : String(error)}`);
    }
    if (response.status === 404) throw new SpecError(`no such workflow: ${idOf(reference)}`);
    if (!response.ok) throw new Error(`could not resolve ${reference}: the endpoint answered ${response.status}`);
    return layersOf(await response.json(), reference);
  };
}
