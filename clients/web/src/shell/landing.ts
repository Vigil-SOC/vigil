/** Home's gate; people without it land on Overview instead. */
export const HOME_PERM = 'ai_decisions.approve'

export const landingScreen = (hasPermission: (perm: string) => boolean): 'home' | 'overview' =>
  hasPermission(HOME_PERM) ? 'home' : 'overview'
