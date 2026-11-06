import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, Field, Popup, Select, SettingsCard, TextInput, Toggle, ToggleRow } from '../../shared/ui'
import { useDetectionRules, type AddSourcePayload, type DetectionSource } from './useSettings'
import type { SectionProps } from './types'

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

// Formats arrive as free-form strings: the short name of a known one, else the raw id.
const formatName = (format: string) =>
  FORMAT_OPTIONS.find((o) => o.value === format)?.label.replace(/ \(.*\)$/, '') ?? format

const EMPTY_SOURCE: AddSourcePayload = {
  name: '',
  source_type: 'git',
  format: 'sigma',
  url: '',
  path: '',
  subdirectory: '',
  story_subdirectory: '',
  deploy_target: false,
}

const fmtNum = (n: number) => n.toLocaleString()

// Formats whose live overlay rules Vigil offers to a customer git repo by pull request.
const DEPLOY_FORMATS = ['splunk', 'elastic']
const deployable = (s: DetectionSource) => s.type === 'git' && DEPLOY_FORMATS.includes(s.format)

// Why a row can't be a deploy target at all; '' when it can.
const notDeployable = (s: DetectionSource) =>
  !DEPLOY_FORMATS.includes(s.format)
    ? `${formatName(s.format)} rules can't be a deploy target; only Splunk and Elastic rules are deployed.`
    : s.type !== 'git'
      ? "A local folder can't be a deploy target; Vigil opens pull requests, so it needs a git repo."
      : ''

// Why a deploy target of this format can't be turned on: another source already is one.
const targetTaken = (format: string, other: DetectionSource | undefined) =>
  other ? `${other.name} is already the ${formatName(format)} deploy target; turn it off first.` : ''

// A deploy target awaiting the person's confirmation: an existing row, or the add form.
type PendingTarget = { name: string; url: string; format: string; source?: DetectionSource }

export default function DetectionRulesPanel({ notify }: SectionProps) {
  const { sources, stats, phase, error, reload, addSource, removeSource, updateSource, updateAll, setDeployTarget } =
    useDetectionRules()
  const [updating, setUpdating] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [form, setForm] = useState<AddSourcePayload>(EMPTY_SOURCE)
  const [saving, setSaving] = useState(false)
  const [confirmDel, setConfirmDel] = useState<DetectionSource | null>(null)
  const [confirmTarget, setConfirmTarget] = useState<PendingTarget | null>(null)

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

  const onDeployTarget = async (s: DetectionSource, on: boolean) => {
    setUpdating(`target:${s.id}`)
    try {
      await setDeployTarget(s.id, on)
      notify('ok', on ? `${s.name} is the ${formatName(s.format)} deploy target.` : `${s.name} is no longer a deploy target.`)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to change the deploy target.')
    } finally {
      setUpdating(null)
    }
  }

  const onConfirmTarget = () => {
    if (!confirmTarget) return
    setConfirmTarget(null)
    if (confirmTarget.source) void onDeployTarget(confirmTarget.source, true)
    else void onAdd()
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
        deploy_target: wantsTarget,
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

  const formDeployable = form.source_type === 'git' && DEPLOY_FORMATS.includes(form.format)
  // The source each format already deploys to (at most one per format; the server refuses a second).
  const targetOf = (format: string) => sources.find((s) => s.deploy_target && s.format === format)
  const formTaken = targetTaken(form.format, targetOf(form.format))
  const wantsTarget = formDeployable && !formTaken && !!form.deploy_target
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
              <tr><th>Rule source</th><th>Format</th><th>Deploy target</th><th>Where</th><th>Rules</th><th aria-label="Actions" /></tr>
            </thead>
            <tbody>
              {sources.map((s) => {
                const where = whereOf(s.git_url || '')
                const updated = s.last_updated ? `Last updated ${new Date(s.last_updated).toLocaleString()}` : ''
                const other = targetOf(s.format)
                const taken = other && other.id !== s.id ? targetTaken(s.format, other) : ''
                return (
                  <tr key={s.id}>
                    <td title={updated || undefined}>
                      <div className="data-name">
                        <b>{s.name}</b>
                        {s.status !== 'ready' && (
                          <span className={`level-pill ${s.status === 'error' ? 'poor' : 'idle'}`}>
                            {s.status === 'not_cloned' ? 'Not cloned' : s.status}
                          </span>
                        )}
                      </div>
                    </td>
                    <td>{formatName(s.format)}</td>
                    <td
                      title={
                        !deployable(s) ? notDeployable(s)
                        : s.vendor_repo ? 'A vendor’s public repo can’t be a deploy target; add your own repo for that.'
                        : taken ||
                          `When one of Vigil's ${formatName(s.format)} rules goes live, an agent opens a pull request adding it here; it waits for approval.`
                      }
                    >
                      {!deployable(s) ? (
                        <span className="text-tx-3" aria-label={notDeployable(s)}>—</span>
                      ) : s.vendor_repo ? (
                        <span className="text-xs text-tx-3">Vendor repo</span>
                      ) : (
                        <Toggle
                          checked={!!s.deploy_target}
                          disabled={!!updating || !!taken}
                          label={`${s.name}: deploy target for ${formatName(s.format)} rules`}
                          title={taken || undefined}
                          onChange={(on) =>
                            on ? setConfirmTarget({ name: s.name, url: s.git_url, format: s.format, source: s }) : onDeployTarget(s, false)
                          }
                        />
                      )}
                    </td>
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
          Sources feed the Security-Detections MCP server. When an agent analyzes findings it searches across{' '}
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
          {formDeployable && (
            <ToggleRow
              label="Deploy target"
              hint={formTaken || `When one of Vigil's ${formatName(form.format)} rules goes live, an agent opens a pull request adding it here.`}
              checked={!formTaken && !!form.deploy_target}
              disabled={!!formTaken}
              onChange={(v) => setForm({ ...form, deploy_target: v })}
            />
          )}
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={() => setAddOpen(false)} disabled={saving}>Cancel</button>
            <button
              className="btn primary"
              onClick={() =>
                wantsTarget
                  ? setConfirmTarget({ name: form.name, url: form.url || '', format: form.format })
                  : onAdd()
              }
              disabled={!addValid || saving}
            >
              {saving ? 'Adding…' : 'Add Source'}
            </button>
          </div>
        </div>
      </Popup>

      {/* Deploy target confirm: Vigil will open pull requests into this repo */}
      <Popup open={!!confirmTarget} onClose={() => setConfirmTarget(null)} title="Make this the deploy target?" width={480}>
        <p className="text-sm text-tx-2 leading-relaxed">
          When one of Vigil's {confirmTarget && formatName(confirmTarget.format)} rules goes live, an agent will open a pull
          request adding it to <strong>{confirmTarget?.name}</strong>:
        </p>
        <p className="text-sm font-mono mt-2 break-all">{confirmTarget?.url}</p>
        <p className="text-sm text-tx-2 leading-relaxed mt-2">
          Each pull request waits for a person's approval. Only choose a repo your organisation owns.
        </p>
        <div className="flex justify-end gap-2.5 mt-5">
          <button className="btn ghost" onClick={() => setConfirmTarget(null)}>Cancel</button>
          <button className="btn primary" onClick={onConfirmTarget}>Make deploy target</button>
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
