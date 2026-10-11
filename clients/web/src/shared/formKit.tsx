import { type ReactNode } from 'react'
import { Icon } from './icons'

export const extractApiError = (e: unknown, fallback: string): string => {
  const err = e as { response?: { data?: { detail?: string } }; message?: string }
  return err?.response?.data?.detail || err?.message || fallback
}

export const Banner = ({ kind, children }: { kind: 'err' | 'ok'; children: ReactNode }) => (
  <div className={`settings-banner ${kind}`}>
    <Icon name={kind === 'err' ? 'alert' : 'check2'} size={14} /> {children}
  </div>
)
