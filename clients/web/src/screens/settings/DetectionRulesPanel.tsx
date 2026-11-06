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
  github_enterprise: false,
}

const fmtNum = (n: number) => n.toLocaleString()

// Formats whose live overlay rules Vigil offers to a customer git repo by pull request.
const DEPLOY_FORMATS = ['splunk', 'elastic']
const deployable = (s: DetectionSource) => s.type === 'git' && DEPLOY_FORMATS.includes(s.format)

// A git URL's host, lowercased: https/ssh URLs and scp-style user@host:path.
const hostOf = (url: string) => {
  const u = url.trim()
  const m = /^[a-z][\w+.-]*:\/\/(?:[^/@]+@)?([^/:]+)/i.exec(u) || /^(?:[^@/]+@)?([^:/]+):/.exec(u)
  return (m?.[1] ?? '').toLowerCase()
}
// Vigil opens deploy pull requests with its GitHub token, so a deploy target is
// github.com, or a host the person says is GitHub Enterprise; the server refuses the rest.
const isGithubCom = (url: string) => hostOf(url) === 'github.com'
// Hosted services that are never GitHub: no GitHub Enterprise mark is offered for them.
const NEVER_GITHUB = ['gitlab.com', 'bitbucket.org', 'dev.azure.com', 'ssh.dev.azure.com', 'codeberg.org', 'gitea.com', 'git.sr.ht']
const neverGithub = (url: string) => NEVER_GITHUB.includes(hostOf(url)) || hostOf(url).endsWith('.visualstudio.com')
const GITHUB_ONLY = 'Only GitHub repos (github.com or GitHub Enterprise) can be deploy targets.'

export default function DetectionRulesPanel({ notify }: SectionProps) {
  const { sources, stats, phase, error, reload, addSource, removeSource, updateSource, updateAll, setDeployTarget } =
    useDetectionRules()
  const [updating, setUpdating] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [form, setForm] = useState<AddSourcePayload>(EMPTY_SOURCE)
  const [saving, setSaving] = useState(false)
  const [confirmDel, setConfirmDel] = useState<DetectionSource | null>(null)
  const [confirmGhe, setConfirmGhe] = useState<DetectionSource | null>(null)

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

  const onDeployTarget = async (s: DetectionSource, on: boolean, githubEnterprise?: boolean) => {
    setUpdating(`target:${s.id}`)
    try {
      await setDeployTarget(s.id, on, githubEnterprise)
      notify('ok', on ? `${s.name} is the ${formatName(s.format)} deploy target.` : `${s.name} is no longer a deploy target.`)
    } catch (e) {
      notify('err', (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || 'Failed to change the deploy target.')
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
        deploy_target: formDeployable && !formNotGithub && !!form.deploy_target,
        github_enterprise: formDeployable && !!form.deploy_target && formGhe && !!form.github_enterprise,
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
  // A deploy target on a host other than github.com: the person must say it is GitHub Enterprise.
  const formNotGithub = neverGithub(form.url || '')
  const formGhe = !!hostOf(form.url || '') && !isGithubCom(form.url || '') && !formNotGithub
  const addValid =
    !!form.name &&
    (form.source_type === 'git' ? !!form.url : !!form.path) &&
    !(formDeployable && form.deploy_target && (formNotGithub || (formGhe && !form.github_enterprise)))

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
                    <td>{formatName(s.format)}</td>
                    <td
                      title={
                        !deployable(s) ? undefined
                        : neverGithub(s.git_url) ? `${GITHUB_ONLY} ${hostOf(s.git_url)} is not GitHub.`
                        : `When one of Vigil's ${formatName(s.format)} rules goes live, an agent opens a pull request adding it here; it waits for approval.`
                      }
                    >
                      {deployable(s) && (
                        <Toggle
                          checked={!!s.deploy_target}
                          disabled={!!updating || (neverGithub(s.git_url) && !s.deploy_target)}
                          label={`${s.name}: deploy target for ${formatName(s.format)} rules`}
                          onChange={(on) =>
                            // A host other than github.com gets the GitHub token only once the person says it is GitHub Enterprise.
                            on && !isGithubCom(s.git_url) && !s.github_enterprise ? setConfirmGhe(s) : onDeployTarget(s, on)
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
              hint={
                formNotGithub
                  ? `${GITHUB_ONLY} ${hostOf(form.url || '')} is not GitHub.`
                  : `When one of Vigil's ${formatName(form.format)} rules goes live, an agent opens a pull request adding it here.`
              }
              checked={!formNotGithub && !!form.deploy_target}
              disabled={formNotGithub}
              onChange={(v) => setForm({ ...form, deploy_target: v })}
            />
          )}
          {formDeployable && form.deploy_target && formGhe && (
            <ToggleRow
              label="This is GitHub Enterprise"
              hint={`Only GitHub repos (github.com or GitHub Enterprise) can be deploy targets: Vigil opens the pull request with its GitHub token, sent to ${hostOf(form.url || '')}/api/v3. Turn this on only if ${hostOf(form.url || '')} runs GitHub Enterprise.`}
              checked={!!form.github_enterprise}
              onChange={(v) => setForm({ ...form, github_enterprise: v })}
            />
          )}
          <div className="flex justify-end gap-2.5 mt-1">
            <button className="btn ghost" onClick={() => setAddOpen(false)} disabled={saving}>Cancel</button>
            <button className="btn primary" onClick={onAdd} disabled={!addValid || saving}>
              {saving ? 'Adding…' : 'Add Source'}
            </button>
          </div>
        </div>
      </Popup>

      {/* GitHub Enterprise confirm: a non-github.com host gets Vigil's GitHub token only once the person says so */}
      <Popup open={!!confirmGhe} onClose={() => setConfirmGhe(null)} title="Is this GitHub Enterprise?" width={480}>
        <p className="text-sm text-tx-2 leading-relaxed">
          Only GitHub repos (github.com or GitHub Enterprise) can be deploy targets. Vigil opens each pull request with its
          GitHub token, so making <strong>{confirmGhe?.name}</strong> the target sends that token to:
        </p>
        <p className="text-sm font-mono mt-2 break-all">{confirmGhe && `${hostOf(confirmGhe.git_url)}/api/v3`}</p>
        <p className="text-sm text-tx-2 leading-relaxed mt-2">
          Confirm only if {confirmGhe && hostOf(confirmGhe.git_url)} runs GitHub Enterprise. GitLab, Bitbucket and other hosts are refused.
        </p>
        <div className="flex justify-end gap-2.5 mt-5">
          <button className="btn ghost" onClick={() => setConfirmGhe(null)}>Cancel</button>
          <button
            className="btn primary"
            onClick={() => {
              const s = confirmGhe
              setConfirmGhe(null)
              if (s) void onDeployTarget(s, true, true)
            }}
          >
            It’s GitHub Enterprise
          </button>
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
