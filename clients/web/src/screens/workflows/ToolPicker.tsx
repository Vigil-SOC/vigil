// Pick the tools an agent or a workflow step may use. Every tool is listed with
// where it comes from and what it does, searchable by either, grouped by the
// integration that serves it; a click adds or removes one, a group adds at once.
import { useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Icon } from '../../shared/icons'
import { CHANGE_WORD, serverLabel, shortName, type CatalogTool, type ToolCatalog } from './toolCatalog'
import './toolPicker.css'

function ChangeMark({ changes }: { changes: string | null }) {
  if (!changes) return <span className="tp-mark none">Not connected</span>
  return <span className={`tp-mark ${changes}`}>{CHANGE_WORD[changes] ?? changes}</span>
}

function ToolIcon({ server, size = 14 }: { server: string; size?: number }) {
  const label = serverLabel(server)
  return <span className={`tp-letter${server === 'vigil' ? ' vigil' : ''}`} style={{ width: size, height: size, fontSize: Math.round(size * 0.55) }} aria-hidden="true">{label.charAt(0)}</span>
}

function matches(t: CatalogTool, q: string): boolean {
  if (!q) return true
  const hay = `${t.name} ${t.description} ${t.group} ${serverLabel(t.server)}`.toLowerCase()
  return q.split(/\s+/).every((w) => hay.includes(w))
}

export default function ToolPicker({
  value,
  onChange,
  catalog,
  suggested = [],
  suggestedLabel = 'Suggested',
  label = 'Tools',
  hint,
}: {
  value: string[]
  onChange: (tools: string[]) => void
  catalog: ToolCatalog
  /** tools offered as one click above the list, e.g. the step's agent's usual tools */
  suggested?: string[]
  suggestedLabel?: string
  label?: string
  hint?: string
}) {
  const [q, setQ] = useState('')
  const [server, setServer] = useState<string>('all')
  const [cursor, setCursor] = useState(0)
  const listRef = useRef<HTMLDivElement>(null)
  const chosen = useMemo(() => new Set(value), [value])
  const needle = q.trim().toLowerCase()

  const servers = useMemo(() => {
    const counts = new Map<string, number>()
    for (const t of catalog.tools) counts.set(t.server, (counts.get(t.server) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => (a[0] === 'vigil' ? -1 : b[0] === 'vigil' ? 1 : serverLabel(a[0]).localeCompare(serverLabel(b[0]))))
  }, [catalog.tools])

  const shown = useMemo(
    () => catalog.tools.filter((t) => (server === 'all' || t.server === server) && matches(t, needle)),
    [catalog.tools, server, needle],
  )
  const groups = useMemo(() => {
    const out: { key: string; title: string; server: string; tools: CatalogTool[] }[] = []
    for (const t of shown) {
      const key = `${t.server}/${t.group}`
      let g = out.find((x) => x.key === key)
      if (!g) {
        g = { key, title: t.server === 'vigil' ? `Vigil · ${t.group}` : serverLabel(t.server), server: t.server, tools: [] }
        out.push(g)
      }
      g.tools.push(t)
    }
    return out
  }, [shown])
  const flat = useMemo(() => groups.flatMap((g) => g.tools), [groups])

  const toggle = (name: string) => onChange(chosen.has(name) ? value.filter((v) => v !== name) : [...value, name])
  const addAll = (names: string[]) => onChange([...value, ...names.filter((n) => !chosen.has(n))])
  const removeAll = (names: string[]) => onChange(value.filter((v) => !names.includes(v)))
  const missingSuggested = suggested.filter((s) => !chosen.has(s))

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setCursor((c) => Math.min(flat.length - 1, c + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setCursor((c) => Math.max(0, c - 1)) }
    else if (e.key === 'Enter') {
      e.preventDefault()
      const t = flat[Math.min(cursor, flat.length - 1)]
      if (t) toggle(t.name)
    } else if (e.key === 'Escape' && q) { e.stopPropagation(); setQ('') }
  }

  return (
    <div className="tp">
      <div className="tp-head">
        <span className="tp-label">{label}</span>
        <span className="tp-count">{value.length === 0 ? 'None yet' : `${value.length} chosen`}</span>
        {value.length > 0 && <button type="button" className="tp-link" onClick={() => onChange([])}>Clear</button>}
      </div>
      {hint && <span className="tp-hint">{hint}</span>}

      {value.length > 0 && (
        <ul className="tp-chosen" aria-label={`Chosen ${label.toLowerCase()}`}>
          {value.map((name) => {
            const t = catalog.byName.get(name)
            return (
              <li key={name} className={`tp-chip${t || catalog.phase !== 'ready' ? '' : ' missing'}`} title={t?.description || name}>
                {t && <ToolIcon server={t.server} size={13} />}
                <span className="tp-chip-name">{name}</span>
                {catalog.phase === 'ready' && !t && <span className="tp-mark none">Not connected</span>}
                {t?.changes === 'on_its_own' && <span className="tp-mark on_its_own">Changes things</span>}
                <button type="button" aria-label={`Remove ${name}`} onClick={() => toggle(name)}><Icon name="close" size={11} /></button>
              </li>
            )
          })}
        </ul>
      )}

      {missingSuggested.length > 0 && (
        <div className="tp-suggest">
          <span>{suggestedLabel}:</span>
          {missingSuggested.slice(0, 8).map((s) => (
            <button key={s} type="button" className="tp-sug" onClick={() => toggle(s)}><Icon name="plus" size={11} /> {s}</button>
          ))}
          {missingSuggested.length > 1 && <button type="button" className="tp-link" onClick={() => addAll(missingSuggested)}>Add all {missingSuggested.length}</button>}
        </div>
      )}

      <div className="tp-search">
        <Icon name="search" size={14} />
        <input
          value={q}
          onChange={(e) => { setQ(e.target.value); setCursor(0) }}
          onKeyDown={onKey}
          placeholder={catalog.phase === 'loading' ? 'Loading tools…' : `Search ${catalog.tools.length} tools by name or what they do`}
          aria-label={`Search ${label.toLowerCase()}`}
          aria-controls="tp-list"
          autoComplete="off"
        />
        {q && <button type="button" className="tp-link" onClick={() => setQ('')}>Clear</button>}
      </div>

      {servers.length > 1 && (
        <div className="tp-servers" role="group" aria-label="Filter by source">
          <button type="button" aria-pressed={server === 'all'} onClick={() => setServer('all')}>All <span>{catalog.tools.length}</span></button>
          {servers.map(([s, n]) => (
            <button key={s} type="button" aria-pressed={server === s} onClick={() => setServer(server === s ? 'all' : s)}>
              <ToolIcon server={s} size={12} /> {serverLabel(s)} <span>{n}</span>
            </button>
          ))}
        </div>
      )}

      <div className="tp-list" id="tp-list" ref={listRef} role="listbox" aria-multiselectable="true" aria-label={`Available ${label.toLowerCase()}`}>
        {catalog.phase === 'error' && <p className="tp-empty">Couldn’t load the tool list. Tools already chosen are kept.</p>}
        {catalog.phase === 'ready' && flat.length === 0 && <p className="tp-empty">No tool matches “{q}”.</p>}
        {groups.map((g) => {
          const names = g.tools.map((t) => t.name)
          const all = names.every((n) => chosen.has(n))
          return (
            <div key={g.key} className="tp-group">
              <div className="tp-group-head">
                <ToolIcon server={g.server} size={13} />
                <span>{g.title}</span>
                <span className="tp-group-n">{g.tools.length}</span>
                <button type="button" className="tp-link" onClick={() => (all ? removeAll(names) : addAll(names))}>{all ? 'Remove all' : `Add all ${g.tools.length}`}</button>
              </div>
              {g.tools.map((t) => {
                const i = flat.indexOf(t)
                const on = chosen.has(t.name)
                return (
                  <div
                    key={t.name}
                    role="option"
                    aria-selected={on}
                    className={`tp-row${on ? ' on' : ''}${i === cursor && needle ? ' cur' : ''}`}
                    onClick={() => toggle(t.name)}
                    onMouseEnter={() => setCursor(i)}
                  >
                    <span className="tp-box" aria-hidden="true">{on && <Icon name="check" size={11} />}</span>
                    <span className="tp-row-main">
                      <span className="tp-row-name">{shortName(t)}</span>
                      {t.description && <span className="tp-row-desc">{t.description}</span>}
                    </span>
                    <ChangeMark changes={t.changes} />
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
    </div>
  )
}
