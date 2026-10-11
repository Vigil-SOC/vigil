type BannerKind = 'ok' | 'err' | 'info'

export interface SectionProps {
  notify: (kind: BannerKind, text: string) => void
}
