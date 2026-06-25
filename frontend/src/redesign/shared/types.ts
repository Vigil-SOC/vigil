/* Shared contract every redesign screen component implements. */
export interface ScreenProps {
  /** open the Vigil chat dock; pass a prompt to auto-send it (used by
      "investigate with Vigil" affordances) */
  openChat: (prompt?: string) => void
  /** tell the shell this screen wants the full-height, non-scrolling view
      (used by the cases / decisions master-detail split layouts) */
  setViewFull: (full: boolean) => void
}
