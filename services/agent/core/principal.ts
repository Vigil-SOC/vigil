// A run's signed principal, as the run bridge sees it: sent beside the shared
// bearer on every call under /internal/runs/{run_id}/, and read here only to
// decide whether the run can go on. Nothing here verifies a signature: reading
// only ever ends a run, and the API is what refuses.

export const REFUSED = "refused: its run credential is missing, names another run, or can't be verified";
export const OUTLIVED = "run outlived its credential";

// Ended a day before its principal expires, so the run's own end still lands.
export const LIFE_MARGIN_MS = 86_400_000;

// The bridge refused the run's own principal. No retry can change that.
export class RunRefused extends Error {
  constructor(reason = REFUSED) {
    super(reason);
    this.name = "RunRefused";
  }
}

export function bridgeHeaders(token: string, principal?: string): Record<string, string> {
  return {
    "content-type": "application/json",
    authorization: `Bearer ${token}`,
    ...(principal === undefined || principal === "" ? {} : { "x-vigil-run-principal": principal }),
  };
}

// The claims a run is judged by, decoded and not verified. null when the token
// is absent or does not decode, which counts as missing.
export function claimsOf(principal: string | undefined): { run_id: string; exp_ms: number } | null {
  const payload = principal?.split(".")[1];
  if (payload === undefined) return null;
  try {
    const claims = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as Record<string, unknown>;
    const { run_id, exp } = claims;
    return typeof run_id === "string" && typeof exp === "number" ? { run_id, exp_ms: exp * 1000 } : null;
  } catch {
    return null;
  }
}
