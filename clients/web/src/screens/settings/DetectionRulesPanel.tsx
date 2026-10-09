import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, Field, Popup, Select, SettingsCard, TextInput } from '../../shared/ui'
import { useDetectionRules, type AddSourcePayload, type DetectionSource } from './useSettings'
import type { SectionProps } from './types'

// Rule formats come back from the detection-rules API as free-form strings, so
// only the known ones get a tint (the `fmt-*` classes, on tokens); any other stays neutral.
const KNOWN_FORMATS = new Set(['sigma', 'splunk', 'elastic', 'kql', 'auto'])
const formatClass = (format: string) => (KNOWN_FORMATS.has(format) ? ` fmt-${format}` : '')

// Show a repo as host/path, the way the board does; local sources keep their path.
const whereOf = (url: string) => url.replace(/^https?:\/\//, '').replace(/\.git$/, '')

const SOURCE_TYPE_OPTIONS = [
  { value: 'git', label: 'Git Repository' },
  { value: 'local', label: 'Local Directory' },
]
const FORMAT_OPTIONS = [
  { value: 'sigma', label: 'Sigma (YAML)' },
  { value: 'splunk', label: 'Splunk ESCU (YAML)' },
  { value: 'elastic', label: 'Elastic (TOML)' },
  { value: 'kql', label: 'KQL (MD/YAML/KQL)' },
  { value: 'auto', label: 'Auto-detect' },
]

const EMPTY_SOURCE: AddSourcePayload = {
  name: '',
  source_type: 'git',
  format: 'sigma',
  url: '',
  path: '',
  subdirectory: '',
  story_subdirectory: '',
}

const fmtNum = (n: number) => n.toLocaleString()

export default function DetectionRulesPanel({ notify }: SectionProps) {
  const { sources, stats, phase, error, reload, addSource, removeSource, updateSource, updateAll } =
    useDetectionRules()
  const [updating, setUpdating] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [form, setForm] = useState<AddSourcePayload>(EMPTY_SOURCE)
  const [saving, setSaving] = useState(false)
  const [confirmDel, setConfirmDel] = useState<DetectionSource | null>(null)

  const title = 'Detection rule sources'
  const desc = 'Rules agents can search and compare against when they explain coverage.'
  if (phase === 'loading' && !stats) { // a reload keeps the table on screen
    return <SettingsCard title={title} desc={desc}><EmptyState compact loading icon="shield" title="Loading detection rules…" /></SettingsCard>
  }
  if (phase === 'error') {
    return (
      <SettingsCard title={title} desc={desc}>
        <EmptyState compact error icon="alert" title="Couldn’t load detection rules" body={error} primary={{ label: 'Retry', onClick: reload, icon: 'refresh' }} />
      </SettingsCard>
    )
  }

  const onUpdateSource = async (id: string) => {
    setUpdating(id)
    try {
      await updateSource(id)
      notify('ok', 'Source updated and MCP server restarted.')
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to update source.')
    } finally {
      setUpdating(null)
    }
  }

  const onUpdateAll = async () => {
    setUpdating('all')
    try {
      const results = await updateAll()
      const ok = results.filter((r) => r.success).length
      notify(ok === results.length ? 'ok' : 'err', `Updated ${ok}/${results.length} sources.`)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to update sources.')
    } finally {
      setUpdating(null)
    }
  }

  const onAdd = async () => {
    setSaving(true)
    try {
      await addSource({
        name: form.name,
        source_type: form.source_type,
        format: form.format,
        url: form.source_type === 'git' ? form.url : undefined,
        path: form.source_type === 'local' ? form.path : undefined,
        subdirectory: form.subdirectory,
        story_subdirectory: form.story_subdirectory,
      })
      notify('ok', `Added source: ${form.name}.`)
      setAddOpen(false)
      setForm(EMPTY_SOURCE)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to add source.')
    } finally {
      setSaving(false)
    }
  }

  const onDelete = async (deleteFiles: boolean) => {
    if (!confirmDel) return
    try {
      await removeSource(confirmDel.id, deleteFiles)
      notify('ok', 'Source removed.')
      setConfirmDel(null)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to remove source.')
    }
  }

  const addValid = !!form.name && (form.source_type === 'git' ? !!form.url : !!form.path)

  return (
    <>
      <SettingsCard
        title={title}
        desc={desc}
        actions={
          <>
            <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
            <button className="btn ghost" onClick={onUpdateAll} disabled={!!updating}>
              <Icon name="download" /> {updating === 'all' ? 'Updating…' : 'Update All'}
            </button>
            <button className="btn ghost" onClick={() => setAddOpen(true)}><Icon name="plus" /> Add source</button>
          </>
        }
      >
        {stats && (
          <div className="flex gap-2 flex-wrap mb-2">
            <span className="chip" style={{ color: 'var(--accent-2)' }}>{fmtNum(stats.total_rules)} total rules</span>
            {Object.entries(stats.by_format).map(([fmt, count]) => (
              <span key={fmt} className={`chip${formatClass(fmt)}`}>{fmt}: {fmtNum(count)}</span>
            ))}
          </div>
        )}

        {sources.length === 0 ? (
          <EmptyState
            compact
            icon="shield"
            title="No detection rule sources configured"
            body="Add a Git repository or local directory, or let the service seed defaults on first load."
            primary={{ label: 'Add source', onClick: () => setAddOpen(true), icon: 'plus' }}
            secondary={{ label: 'Refresh', onClick: reload, icon: 'refresh' }}
          />
        ) : (
          <table className="tbl data-table data-rules">
            <thead>
              <tr><th>Rule source</th><th>Format</th><th>Where</th><th>Rules</th><th aria-label="Actions" /></tr>
            </thead>
            <tbody>
              {sources.map((s) => {
                const where = whereOf(s.git_url || '')
                const updated = s.last_updated ? `Last updated ${new Date(s.last_updated).toLocaleString()}` : ''
                return (
                  <tr key={s.id}>
                    <td className="data-clip" title={updated || undefined}>
                      <b>{s.name}</b>
                      {s.status !== 'ready' && (
                        <span className={`level-pill ${s.status === 'error' ? 'poor' : 'idle'} ml-2`}>
                          {s.status === 'not_cloned' ? 'Not cloned' : s.status}
                        </span>
                      )}
                    </td>
                    <td><span className={`chip${formatClass(s.format)}`}>{s.format}</span></td>
                    <td className="data-clip font-mono" title={s.git_url || undefined}>{where || '—'}</td>
                    <td>{fmtNum(s.rule_count)}</td>
                    <td className="data-act">
                      <button className="data-link" disabled={!!updating} onClick={() => onUpdateSource(s.id)}>
                        {updating === s.id ? 'Working…' : s.status === 'not_cloned' ? 'Clone' : 'Update'}
                      </button>
                      <button className="btn ghost icon" title="Remove source" aria-label={`Remove ${s.name}`} onClick={() => setConfirmDel(s)}>
                        <Icon name="trash" size={15} />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}

        <p className="text-xs text-tx-3 mt-3">
          Sources feed the Security-Detections MCP server. When Claude analyzes findings it searches across{' '}
          {stats ? fmtNum(stats.total_rules) : '…'} rules. Updating a source restarts the MCP server to rebuild its index.
        </p>
      </SettingsCard>

      {/* Add source dialog */}
      <Popup open={addOpen} onClose={() => setAddOpen(false)} title="Add Detection Rule Source" width={480}>
        <div className="flex flex-col gap-3.5">
          <Field label="Source Name">
            <TextInput value={form.name} placeholder="e.g. My Custom Rules" onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Source Type">
            <Select value={form.source_type} options={SOURCE_TYPE_OPTIONS} onSelect={(v) => setForm({ ...form, source_type: v as 'git' | 'local' })} />
          </Field>
          <Field label="Rule Format">
            <Select value={form.format} options={FORMAT_OPTIONS} onSelect={(v) => setForm({ ...form, format: v as AddSourcePayload['format'] })} />
          </Field>
          {form.source_type === 'git' ? (
            <Field label="Git Repository URL">
              <TextInput value={form.url} placeholder="https://github.com/org/repo.git" onChange={(e) => setForm({ ...form, url: e.target.value })} />
            </Field>
          ) : (
            <Field label="Local Directory Path">
              <TextInput value={form.path} placeholder="/path/to/rules" onChange={(e) => setForm({ ...form, path: e.target.value })} />
            </Field>
          )}
          <Field label="Subdirectory (optional)" hint="Subdirectory within the repo/path that contains the rules.">
            <TextInput value={form.subdirectory} placeholder="e.g. rules" onChange={(e) => setForm({ ...form, subdirectory: e.target.value })} />
          </Field>
          {form.format === 'splunk' && (
            <Field label="Story Subdirectory (optional)" hint="Subdirectory for Splunk story files.">
              <TextInput value={form.story_subdirectory} placeholder="e.g. stories" onChange={(e) => setForm({ ...form, story_subdirectory: e.target.value })} />
            </Field>
          )}
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={() => setAddOpen(false)} disabled={saving}>Cancel</button>
            <button className="btn primary" onClick={onAdd} disabled={!addValid || saving}>
              {saving ? 'Adding…' : 'Add Source'}
            </button>
          </div>
        </div>
      </Popup>

      {/* Delete confirm — two destructive options */}
      <Popup open={!!confirmDel} onClose={() => setConfirmDel(null)} title="Remove Detection Rule Source" width={440}>
        <p className="text-sm text-tx-2 leading-relaxed">
          Remove <strong>{confirmDel?.name}</strong> from detection rule sources?
        </p>
        <div className="flex justify-end gap-2.5 mt-5">
          <button className="btn ghost" onClick={() => setConfirmDel(null)}>Cancel</button>
          <button className="btn ghost" onClick={() => onDelete(false)}>Remove (keep files)</button>
          <button className="btn danger" onClick={() => onDelete(true)}>Remove &amp; delete files</button>
        </div>
      </Popup>
    </>
  )
}
