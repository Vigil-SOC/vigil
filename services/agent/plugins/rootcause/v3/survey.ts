// The survey's listing: every source with its event count, time range and field
// names, one domain per source. These read it back; discovery searches the source itself.
import type { Domain } from "./types.js";

export interface Survey { text: string; domains: Domain[]; sizes: Record<Domain, number>; manualEnvironment?: boolean }

export const domainsIn = (text: string): Domain[] => [...text.matchAll(/^- ([^\s:][^\n]*?): [\d,]+ events/gm)].map((m) => m[1]!);
export const sizesIn = (text: string): Record<Domain, number> => Object.fromEntries([...text.matchAll(/^- ([^\s:][^\n]*?): ([\d,]+) events/gm)].map((m) => [m[1]!, Number(m[2]!.replace(/,/g, ""))]));
