/* ============================================================
   Settings · Integrations · Detection Rules — manage the detection
   rule sources (git repos / local dirs) fed to the Security-Detections
   MCP server. Mirrors DetectionRulesTab.tsx.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, Popup, Select, TextInput } from '../../shared/ui'
import { useDetectionRules, type AddSourcePayload, type DetectionSource } from './useSettings'
import type { SectionProps } from './types'

const FORMAT_COLORS: Record<string, string> = {
  sigma: '#2196f3',
  splunk: '#4caf50',
  elastic: '#ff9800',
  kql: '#9c27b0',
  auto: '#607d8b',
}

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

  if (phase === 'loading') return <div className="text-sm text-tx-3 py-16 text-center">Loading detection rules…</div>
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load detection rules: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
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
    <div className="flex flex-col gap-4" style={{ maxWidth: 1100 }}>
      <div className="flex items-start gap-3 flex-wrap">
        <div className="flex-1 min-w-0">
          <h3 className="text-[14.5px] font-semibold text-tx flex items-center gap-2">
            <Icon name="shield" size={16} /> Detection Rule Sources
          </h3>
          {stats && (
            <div className="flex gap-2 flex-wrap mt-2">
              <span className="chip" style={{ color: 'var(--accent-2)' }}>{fmtNum(stats.total_rules)} total rules</span>
              {Object.entries(stats.by_format).map(([fmt, count]) => (
                <span key={fmt} className="chip" style={{ color: FORMAT_COLORS[fmt] || 'var(--tx-3)' }}>
                  {fmt}: {fmtNum(count)}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex gap-2">
          <button className="btn ghost" onClick={reload}><Icon name="refresh" /> Refresh</button>
          <button className="btn ghost" onClick={onUpdateAll} disabled={!!updating}>
            <Icon name="download" /> {updating === 'all' ? 'Updating…' : 'Update All'}
          </button>
          <button className="btn primary" onClick={() => setAddOpen(true)}><Icon name="plus" /> Add Source</button>
        </div>
      </div>

      {sources.length === 0 ? (
        <div className="settings-banner info">
          <Icon name="info" size={14} />
          <span>No detection rule sources configured. Click “Add Source”, or the service seeds defaults on first load.</span>
        </div>
      ) : (
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))' }}>
          {sources.map((s) => (
            <div key={s.id} className="card card-sq p-3.5 flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <Icon name={s.type === 'git' ? 'fork' : 'folder'} size={15} />
                <span className="text-[13px] font-semibold text-tx flex-1 truncate">{s.name}</span>
                <span
                  className={`status ${s.status === 'ready' ? 'closed' : s.status === 'error' ? 'open' : 'investigating'}`}
                >
                  {s.status}
                </span>
              </div>
              <div className="flex gap-2 flex-wrap">
                <span className="chip" style={{ color: FORMAT_COLORS[s.format] || 'var(--tx-3)' }}>{s.format}</span>
                <span className="chip">{fmtNum(s.rule_count)} rules</span>
              </div>
              {s.git_url && <div className="text-xs text-tx-3 break-all font-mono">{s.git_url}</div>}
              {s.last_updated && (
                <div className="text-xs text-tx-faint">Last updated: {new Date(s.last_updated).toLocaleString()}</div>
              )}
              <div className="flex items-center justify-between mt-auto pt-1">
                <button className="btn ghost" disabled={!!updating} onClick={() => onUpdateSource(s.id)}>
                  <Icon name="download" /> {updating === s.id ? 'Working…' : s.status === 'not_cloned' ? 'Clone' : 'Update'}
                </button>
                <button className="btn ghost icon" title="Remove source" onClick={() => setConfirmDel(s)}>
                  <Icon name="trash" size={15} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="settings-banner info">
        <Icon name="info" size={14} />
        <span>
          Sources feed the Security-Detections MCP server. When Claude analyzes findings it searches across{' '}
          {stats ? fmtNum(stats.total_rules) : '…'} rules. Updating a source restarts the MCP server to rebuild its index.
        </span>
      </div>

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
    </div>
  )
}
