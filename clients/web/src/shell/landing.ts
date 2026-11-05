/** Home's gate; people without it land on Overview instead. */
export const HOME_PERM = 'ai_decisions.approve'

/** Day one setup's gate: building a baseline and going live are writes to findings. */
export const START_PERM = 'findings.write'

/** Going live from a source turns it on, so the route asks this as well. */
export const GO_LIVE_SOURCE_PERM = 'integrations.write'

/** Only someone who can finish setup is sent there. Others open it from the nav. */
export const landsOnStart = (hasPermission: (perm: string) => boolean): boolean =>
  hasPermission(START_PERM) && hasPermission(GO_LIVE_SOURCE_PERM)

export const landingScreen = (hasPermission: (perm: string) => boolean): 'home' | 'overview' =>
  hasPermission(HOME_PERM) ? 'home' : 'overview'
