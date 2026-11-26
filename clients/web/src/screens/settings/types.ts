import type { ConsoleScreenProps } from '../../shared/types'

type BannerKind = 'ok' | 'err' | 'info'

export interface SectionProps {
  notify: (kind: BannerKind, text: string) => void
  /** The console's chat dock, for a section that starts a conversation. */
  openChat?: ConsoleScreenProps['openChat']
}

/** The Directory pushes card on Team & access, which the SSO card's sync status links to. */
export const DIRECTORY_PUSHES_ID = 'directory-pushes'
