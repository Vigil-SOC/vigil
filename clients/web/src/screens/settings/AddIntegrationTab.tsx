import { useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, TextInput } from '../../shared/ui'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import { CATEGORY_ICONS, categoryCounts, type CatalogEntry } from './integrationCatalog'

interface Props {
  entries: CatalogEntry[]
  busy: string | null
  onConnect: (integration: IntegrationMetadata) => void
  /** a server with no setup fields has nothing to configure: it is only switched on */
  onTurnOn: (server: string) => void
  onRefresh: () => void
}

/** Add integration: category chips with counts over a card grid of the client catalog. */
export default function AddIntegrationTab({ entries, busy, onConnect, onTurnOn, onRefresh }: Props) {
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('All')
  const counts = useMemo(() => categoryCounts(entries), [entries])
  // a chip that vanished (its integrations were all removed) falls back to All
  const active = counts.some(([l]) => l === category) ? category : 'All'
  const q = search.trim().toLowerCase()
  const shown = entries.filter(
    (e) => (active === 'All' || e.category === active) && (!q || `${e.name} ${e.description} ${e.category}`.toLowerCase().includes(q)),
  )

  return (
    <>
      <div className="flex items-center gap-3 flex-wrap">
        <div className="search" style={{ flex: 1, minWidth: 220, maxWidth: 420 }}>
          <Icon name="search" size={15} />
          <TextInput placeholder="Search integrations…" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
        <button className="btn ghost" onClick={onRefresh}><Icon name="refresh" /> Refresh</button>
      </div>

      {entries.length > 0 && (
        <div className="int-cats" role="group" aria-label="Category">
          {[['All', entries.length] as [string, number], ...counts].map(([label, n]) => (
            <button
              key={label}
              type="button"
              className="int-cat"
              aria-pressed={active === label}
              onClick={() => setCategory(label)}
            >
              {label}
              <span className="int-cat-n">{n}</span>
            </button>
          ))}
        </div>
      )}

      {entries.length === 0 && (
        <EmptyState compact icon="link" title="Nothing to add" body="This console offers no integrations." />
      )}
      {entries.length > 0 && shown.length === 0 && (
        <EmptyState
          compact
          icon="filter"
          title="No integrations match"
          body={q ? `Nothing in ${active === 'All' ? 'the catalog' : active} matches “${search.trim()}”.` : undefined}
          primary={{ label: 'Clear filters', onClick: () => { setSearch(''); setCategory('All') }, icon: 'close' }}
        />
      )}
      {shown.length > 0 && (
        <div className="int-grid">
          {shown.map((e) => (
            <div key={e.key} className="int-card">
              <div className="int-card-head">
                <span className="int-card-ic" aria-hidden><Icon name={CATEGORY_ICONS[e.category] ?? 'grid'} size={15} /></span>
                <span className="int-card-name" title={e.name}>{e.name}</span>
                <span className={`int-card-state${e.connected ? ' on' : ''}`}>{e.connected ? 'Connected' : 'Available'}</span>
              </div>
              <span className="int-card-cat">{e.category}</span>
              <p className="int-clamp int-card-desc" title={e.description}>{e.description}</p>
              {!e.connected && (
                <button
                  className="btn int-act int-card-cta"
                  disabled={busy === e.server}
                  aria-label={`${e.integration ? 'Connect' : 'Turn on'} ${e.name}`}
                  onClick={() => (e.integration ? onConnect(e.integration) : onTurnOn(e.server))}
                >
                  {e.integration ? 'Connect' : 'Turn on'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  )
}
