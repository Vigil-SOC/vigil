import { reportStamp } from './stamp'

/** The site module's stored body: sources read and each declared item's state. No host is kept. */
type Source = {
  source: string
  state: 'read' | 'not_checked'
  reason?: string
  listed?: number
  undeclared?: number
}
type Item = {
  kind: 'zone' | 'crown_jewel' | 'owner' | 'blind_spot'
  name?: string | null
  file?: string
  line?: number
  state: 'matched' | 'declared_not_observed' | 'not_evaluated'
  reason?: string
  source?: string
  heard_at?: string
  seen?: boolean
  place?: string
  owners?: string[]
}
type Body = { not_checked?: string; checked_at?: string; sources?: Source[]; items?: Item[] }

const KIND_WORD: Record<Item['kind'], string> = {
  zone: 'Zone',
  crown_jewel: 'Crown jewel',
  owner: 'Owner',
  blind_spot: 'Blind spot',
}

const STATE_WORD: Record<Item['state'], string> = {
  matched: 'Matched',
  declared_not_observed: 'Declared but not observed',
  not_evaluated: 'Not evaluated',
}

/** Each source's word for a host and its caveat, as its estate checks put them. */
const SOURCE_WORDS: Record<string, { noun: string; note?: string }> = {
  Wazuh: { noun: 'agents', note: 'The manager is not counted.' },
  CrowdStrike: { noun: 'endpoints' },
  Defender: {
    noun: 'endpoints',
    note: "Defender's last seen is its last full report, and can be a day behind the console.",
  },
}

function sourceLine(source: Source): string {
  // A record stored before a source was read carries another state: never shown as read.
  if (source.state !== 'read') return `${source.source}: ${(source.reason ?? 'not read').replace(/\.$/, '')}.`
  const words = SOURCE_WORDS[source.source] ?? { noun: 'hosts' }
  const parts = [`${words.noun} listed ${source.listed ?? 0}`]
  if (typeof source.undeclared === 'number') parts.push(`in no zone ${source.undeclared}`)
  return `${source.source}: read; ${parts.join(', ')}.${words.note ? ` ${words.note}` : ''}`
}

function itemFacts(item: Item): string {
  const parts = [STATE_WORD[item.state] ?? item.state]
  if (item.reason) parts.push(item.reason.replace(/^not checked: /, ''))
  if (item.source) parts.push(item.source)
  if (item.heard_at) parts.push(`Last heard ${reportStamp(item.heard_at)}`)
  if (item.kind === 'crown_jewel' && item.state !== 'not_evaluated') {
    parts.push(item.seen ? 'Heard within 7 days' : 'Not heard within 7 days')
    if (item.place === 'inside') parts.push('In its zone')
    else if (item.place === 'outside') parts.push('Outside its zone')
    else if (item.place && item.seen) parts.push(`Place ${item.place}`)
  }
  if (item.owners) parts.push(item.owners.length > 0 ? `Owner ${item.owners.join(', ')}` : 'No active owner')
  if (item.file) parts.push(`${item.file}:${item.line ?? ''}`)
  return parts.join(' · ')
}

export function SiteItems({ evidence }: { evidence: unknown }) {
  if (!evidence || typeof evidence !== 'object') return null
  const body = evidence as Body
  const sources = body.sources ?? []
  const items = body.items ?? []
  return (
    <>
      {body.not_checked && <p className="d1-quiet">{body.not_checked}.</p>}
      {sources.length > 0 && (
        <>
          <p className="d1-sub">Sources</p>
          <ul className="d1-gaps">
            {sources.map((source) => (
              <li key={source.source}>{sourceLine(source)}</li>
            ))}
          </ul>
        </>
      )}
      {items.length > 0 && (
        <>
          <p className="d1-sub">Declared items</p>
          <div className="d1-rows">
            {items.map((item) => (
              <div className="d1-row" key={`${item.kind}-${item.file}-${item.line}`}>
                <p>
                  <b>{KIND_WORD[item.kind] ?? item.kind}</b>
                  {item.name ? ` ${item.name}` : ''}
                </p>
                <p className="d1-facts">{itemFacts(item)}</p>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  )
}
