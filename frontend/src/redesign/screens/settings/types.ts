/* Shared contract for Settings section components. */
export type BannerKind = 'ok' | 'err' | 'info'

export interface SectionProps {
  /** surface a transient banner at the top of the settings content area */
  notify: (kind: BannerKind, text: string) => void
}
