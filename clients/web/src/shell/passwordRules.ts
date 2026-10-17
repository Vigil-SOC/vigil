/** What the change-password form can check before the server sees it. */

/** The server's default floor (AUTH_MIN_PASSWORD_LENGTH). The server still has the last word. */
export const MIN_PASSWORD_LENGTH = 12
/** bcrypt reads at most 72 bytes; the server refuses anything longer. */
export const MAX_PASSWORD_BYTES = 72

export interface PasswordForm {
  current: string
  next: string
  confirm: string
}

export interface PasswordProblems {
  current?: string
  next?: string
  confirm?: string
}

/** The checks the browser can make before asking the server. Strength and reuse are left to the server. */
export function passwordProblems({ current, next, confirm }: PasswordForm): PasswordProblems {
  const problems: PasswordProblems = {}
  if (!current) problems.current = 'Enter your current password.'
  if (next.length < MIN_PASSWORD_LENGTH) {
    const more = MIN_PASSWORD_LENGTH - next.length
    problems.next = `Use at least ${MIN_PASSWORD_LENGTH} characters (${more} more).`
  } else if (new TextEncoder().encode(next).length > MAX_PASSWORD_BYTES) {
    problems.next = 'That is too long. Use a shorter passphrase.'
  } else if (current && next === current) {
    problems.next = 'Choose a password different from your current one.'
  }
  if (confirm !== next) problems.confirm = 'This does not match the new password.'
  return problems
}
