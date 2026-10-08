import { InfoTip } from './InfoTip'
import './kit.css'

/** The placeholder for a figure Vigil does not record yet; `tip` adds the ⓘ that says why. */
export function NotMeasured({ label, tip, className, align = 'start' }: { label?: string; tip?: string; className?: string; align?: 'start' | 'end' }) {
  return (
    <span className={`not-measured${className ? ` ${className}` : ''}`}>
      {label ? `${label}: ` : ''}Not measured yet
      {tip && <InfoTip label={tip} text={tip} align={align} />}
    </span>
  )
}
