import type { Finding } from '../../data/data'
import { Icon } from '../../shared/icons'

export default function FindingVStrikeAction({ finding, onOpen }: { finding: Finding; onOpen: (finding: Finding) => void }) {
  const valid = Boolean(finding.sourceIp && finding.destinationIp)
  return <button className="btn ghost finding-vstrike" disabled={!valid}
    aria-label={`View ${finding.id} endpoints in VStrike`}
    title={valid ? 'Open source and destination in VStrike' : 'A single source and destination IPv4 address are required'}
    onClick={(event) => { event.stopPropagation(); onOpen(finding) }}><Icon name="graph" size={14} />VStrike</button>
}
