// A privileged change the server refused until the person confirms it's them (#4179).
// The response interceptor in api.ts hands the refusal to the one dialog the console
// mounts (shell/ConfirmItDialog), then retries the request once it is confirmed.
export const CONFIRMATION_REQUIRED = 'confirmation_required'
// The person has no two-step sign-in yet: set it up, then the change goes through.
export const STEP_UP_MFA_SETUP = 'step_up_mfa_setup'

export type StepUpDetail = {
  code: typeof CONFIRMATION_REQUIRED | typeof STEP_UP_MFA_SETUP
  /** the action the dialog's title names: "change settings" */
  action: string
  /** one_factor: a code (or the password with no MFA); strong: password and code;
   *  sso: no password and no MFA, signed in through the provider: confirm there (#4285) */
  variant?: 'one_factor' | 'strong' | 'sso'
  /** sso: the provider's host, for "Confirm at …" */
  provider?: string | null
  window_minutes?: number
  confirmed_at?: string | null
}

/** Resolves once the person is confirmed, rejects if they cancel. */
type Asker = (detail: StepUpDetail) => Promise<void>
let asker: Asker | null = null

export function registerStepUp(fn: Asker): () => void {
  asker = fn
  return () => {
    if (asker === fn) asker = null
  }
}

export function stepUpDetailOf(error: unknown): StepUpDetail | null {
  const e = error as { response?: { status?: number; data?: { detail?: Partial<StepUpDetail> } } } | null
  const detail = e?.response?.data?.detail
  if (e?.response?.status !== 403 || typeof detail !== 'object' || !detail) return null
  return detail.code === CONFIRMATION_REQUIRED || detail.code === STEP_UP_MFA_SETUP ? (detail as StepUpDetail) : null
}

export function askStepUp(detail: StepUpDetail): Promise<void> {
  return asker ? asker(detail) : Promise.reject(new Error('no confirmation dialog'))
}
