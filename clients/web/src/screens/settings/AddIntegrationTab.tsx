import { useMemo, useState } from 'react'
import { Icon } from '../../shared/icons'
import { FilterChip } from '../../shared/FilterChip'
import { EmptyState, TextInput } from '../../shared/ui'
import type { IntegrationMetadata } from '../../config/integrationSchema'
import { categoryLabels, type CatalogEntry } from './integrationCatalog'

interface Props {
  entries: CatalogEntry[]
  busy: string | null
  onConnect: (integration: IntegrationMetadata) => void
  /** a server with no setup fields has nothing to configure: it is only switched on */
  onTurnOn: (server: string) => void
  onRefresh: () => void
}

/** Add integration: category chips and search over a card grid of the client catalog. */
export default function AddIntegrationTab({ entries, busy, onConnect, onTurnOn, onRefresh }: Props) {
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('All')
  const labels = useMemo(() => categoryLabels(entries), [entries])
  // a chip that vanished (its integrations were all removed) falls back to All
  const active = labels.includes(category) ? category : 'All'
  const q = search.trim().toLowerCase()
  const shown = entries.filter(
    (e) => (active === 'All' || e.category === active) && (!q || `${e.name} ${e.description} ${e.category}`.toLowerCase().includes(q)),
  )

  return (
    <>
      <div className="int-filters">
        {entries.length > 0 && (
          <div className="int-cats" role="group" aria-label="Category">
            {['All', ...labels].map((label) => (
              <FilterChip key={label} label={label} active={active === label} onClick={() => setCategory(label)} />
            ))}
          </div>
        )}
        <div className="int-filters-end">
          <div className="search">
            <Icon name="search" size={15} />
            <TextInput placeholder="Search integrations…" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <button className="btn ghost" onClick={onRefresh}><Icon name="refresh" /> Refresh</button>
        </div>
      </div>

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
                <span className="int-card-name" title={e.name}>{e.name}</span>
                <span className={`int-card-state${e.connected ? ' on' : ''}`}>{e.connected ? 'Connected' : 'Available'}</span>
              </div>
              <p className="int-card-line" title={`${e.category} · ${e.description}`}>{e.category} · {e.description}</p>
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
