import { useCallback, useEffect, useState } from 'react'
import api, { ssoApi, type SsoSettings, type SsoSettingsBody } from '../../services/api'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { Field, PasswordInput, Select, SettingsCard, TextInput, Toggle } from '../../shared/ui'
import type { Role } from './useSettings'
import type { SectionProps } from './types'

type Mapping = { group: string; role_id: string }
type Form = {
  enabled: boolean
  discovery_url: string
  client_id: string
  client_secret: string
  group_roles: Mapping[]
  default_role_id: string
  groups_claim: string
  require_sso: boolean
}

const toForm = (s: SsoSettings): Form => ({
  enabled: !!s.enabled,
  discovery_url: s.discovery_url ?? '',
  client_id: s.client_id ?? '',
  client_secret: '',
  group_roles: (s.group_roles ?? []).map((g) => ({ ...g })),
  default_role_id: s.default_role_id ?? 'role-viewer',
  groups_claim: s.groups_claim ?? 'groups',
  require_sso: !!s.require_sso,
})

const same = (a: Form, b: Form) => JSON.stringify(a) === JSON.stringify(b)

/** Settings › Team & access: the single sign-on provider (GET/PUT /api/auth/sso/settings).
 * A save that changes the provider is checked against its discovery document by the server;
 * the group → role list appears once one has reached it. Shown only to roles.manage. */
export default function SsoCard({ notify }: SectionProps) {
  const [saved, setSaved] = useState<SsoSettings | null>(null)
  const [form, setForm] = useState<Form | null>(null)
  const [roles, setRoles] = useState<Role[]>([])
  const [failed, setFailed] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [replacing, setReplacing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)

  const load = useCallback(() => {
    setFailed(null)
    ssoApi.get()
      .then((res) => { setSaved(res.data); setForm(toForm(res.data)); setReplacing(false); setProblem(null) })
      .catch((e) => setFailed(errorText(e, 'Could not load the single sign-on settings.')))
    // Every role, custom ones too; without users.read the selects show only the ids saved.
    api.get('/users/roles/list').then((res) => setRoles(res.data?.roles ?? [])).catch(() => setRoles([]))
  }, [])
  useEffect(load, [load])

  if (failed) {
    return (
      <SettingsCard wide title="Single sign-on">
        <div className="settings-banner err" role="alert">
          <Icon name="alert" size={14} />
          <span>{failed}</span>
          <span className="grow" />
          <button className="btn ghost" onClick={load}>Retry</button>
        </div>
      </SettingsCard>
    )
  }
  if (!saved || !form) {
    return (
      <SettingsCard wide title="Single sign-on">
        <div className="text-sm text-tx-3">Loading single sign-on settings…</div>
      </SettingsCard>
    )
  }

  const set = (patch: Partial<Form>) => setForm({ ...form, ...patch })
  const setRow = (i: number, patch: Partial<Mapping>) =>
    set({ group_roles: form.group_roles.map((g, j) => (j === i ? { ...g, ...patch } : g)) })
  const move = (i: number, by: number) => {
    const rows = [...form.group_roles]
    ;[rows[i], rows[i + by]] = [rows[i + by], rows[i]]
    set({ group_roles: rows })
  }

  const reached = !!saved.issuer
  // While required, the provider and SSO itself stay as saved: turn Require off first.
  const locked = !!saved.require_sso
  const lockedTitle = locked ? 'Turn off Require single sign-on first' : undefined
  const blockers = saved.require_blockers ?? []
  const dirty = !same(form, toForm(saved))
  const valid = form.group_roles.every((g) => g.group.trim()) && !!form.groups_claim.trim()
  // A saved id with no role behind it (or a list we could not read) still shows as itself.
  const roleOptions = (current: string) => {
    const opts = roles.map((r) => ({ value: r.role_id, label: r.name }))
    return current && !opts.some((o) => o.value === current) ? [...opts, { value: current, label: current }] : opts
  }

  const save = async () => {
    setBusy(true)
    setProblem(null)
    const body: SsoSettingsBody = {
      ...form,
      client_secret: form.client_secret.trim() || null,
      group_roles: form.group_roles.map((g) => ({ group: g.group.trim(), role_id: g.role_id })),
    }
    try {
      const res = await ssoApi.put(body, saved.version)
      setSaved(res.data)
      setForm(toForm(res.data))
      setReplacing(false)
      notify('ok', res.data.issuer ? 'Single sign-on saved. The provider answered.' : 'Single sign-on saved.')
    } catch (e) {
      const r = (e as { response?: { status?: number; data?: { current?: SsoSettings } } })?.response
      if (r?.status === 412 && r.data?.current) {
        setSaved(r.data.current)
        setProblem('These settings changed after you opened them, so nothing was saved. Your edits are kept; save again to replace them.')
      } else {
        setProblem(errorText(e, 'The single sign-on settings were not saved.'))
      }
    } finally {
      setBusy(false)
    }
  }

  const copy = () => {
    void navigator.clipboard?.writeText(saved.redirect_url).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <SettingsCard
      wide
      title="Single sign-on"
      desc="People sign in through your company's identity provider, using OpenID Connect."
      actions={
        <span className="flex items-center gap-2 text-xs text-tx-3">
          {form.enabled ? 'On' : 'Off'}
          <Toggle
            checked={form.enabled}
            label="Single sign-on"
            title={lockedTitle ?? (reached ? undefined : 'Save settings that reach the provider first')}
            disabled={busy || locked || (!reached && !form.enabled)}
            onChange={(v) => set({ enabled: v })}
          />
        </span>
      }
    >
      <div className="sso-card">
        {problem && (
          <div className="settings-banner err" role="alert">
            <Icon name="alert" size={14} />
            <span>{problem}</span>
          </div>
        )}
        <div className="field">
          <label className="field-label" htmlFor="sso-discovery">Discovery URL</label>
          <TextInput
            id="sso-discovery"
            className="mono"
            placeholder="https://your-provider/.well-known/openid-configuration"
            value={form.discovery_url}
            disabled={busy || locked}
            title={lockedTitle}
            spellCheck={false}
            onChange={(e) => set({ discovery_url: e.target.value })}
          />
          {reached && (
            <span className="sso-reached">
              <Icon name="check" size={12} /> Reached when last saved. Issuer: {saved.issuer}
            </span>
          )}
        </div>
        <div className="sso-pair">
          <Field label="Client ID">
            <TextInput className="mono" value={form.client_id} disabled={busy || locked} title={lockedTitle} spellCheck={false} onChange={(e) => set({ client_id: e.target.value })} />
          </Field>
          {saved.client_secret_set && !replacing ? (
            <Field label="Client secret" hint="Saved. It is never shown again.">
              <span className="sso-line">
                <TextInput className="mono" value="••••••••••••••••" readOnly aria-label="Client secret, saved" tabIndex={-1} />
                <button type="button" className="btn ghost" disabled={busy} onClick={() => setReplacing(true)}>Replace</button>
              </span>
            </Field>
          ) : (
            <Field
              label="Client secret"
              hint={saved.client_secret_set ? 'Leave empty to keep the saved secret.' : undefined}
            >
              <PasswordInput
                value={form.client_secret}
                disabled={busy}
                autoComplete="new-password"
                onChange={(e) => set({ client_secret: e.target.value })}
              />
            </Field>
          )}
        </div>
        <div className="field">
          <span className="field-label">Redirect address</span>
          <span className="sso-line">
            <code className="sso-redirect">{saved.redirect_url}</code>
            <button type="button" className="btn ghost" onClick={copy}>
              <Icon name={copied ? 'check' : 'copy'} size={14} /> {copied ? 'Copied' : 'Copy'}
            </button>
          </span>
          <span className="field-hint">
            {saved.frontend_url_set
              ? 'Register this address at your identity provider. It comes from VIGIL_FRONTEND_URL.'
              : 'Register this address at your identity provider first, then paste the three values it gives you.'}
          </span>
        </div>
        {!saved.frontend_url_set && (
          <div className="settings-banner warn" role="note">
            <Icon name="alert" size={14} />
            <span>
              This is a local address because <code>VIGIL_FRONTEND_URL</code> isn't set. It only works on this machine. Before other
              people sign in, set it to the address they use and restart.
            </span>
          </div>
        )}

        {reached && (
          <>
            <hr className="sso-rule" />
            <div className="sso-sub">
              <h4>Roles from groups</h4>
              <p>Checked top to bottom at each sign-in. The first match wins.</p>
            </div>
            {form.group_roles.length > 0 && (
              <ol className="sso-map">
                {form.group_roles.map((g, i) => (
                  <li key={i} className="sso-map-row">
                    <span className="sso-map-order">
                      <button type="button" className="btn ghost icon" aria-label="Move up" disabled={busy || i === 0} onClick={() => move(i, -1)}>
                        <Icon name="arrowUp" size={13} />
                      </button>
                      <button
                        type="button"
                        className="btn ghost icon"
                        aria-label="Move down"
                        disabled={busy || i === form.group_roles.length - 1}
                        onClick={() => move(i, 1)}
                      >
                        <Icon name="arrowDn" size={13} />
                      </button>
                    </span>
                    <span className="sso-map-n">{i + 1}</span>
                    <TextInput
                      className="mono"
                      placeholder="Group name"
                      aria-label={`Group ${i + 1}`}
                      value={g.group}
                      disabled={busy}
                      spellCheck={false}
                      onChange={(e) => setRow(i, { group: e.target.value })}
                    />
                    <Icon name="arrowR" size={14} />
                    <Select label={`Role for group ${i + 1}`} value={g.role_id} options={roleOptions(g.role_id)} disabled={busy} onSelect={(v) => setRow(i, { role_id: v })} />
                    <button
                      type="button"
                      className="btn ghost icon"
                      aria-label={`Remove group ${i + 1}`}
                      disabled={busy}
                      onClick={() => set({ group_roles: form.group_roles.filter((_, j) => j !== i) })}
                    >
                      <Icon name="close" size={13} />
                    </button>
                  </li>
                ))}
              </ol>
            )}
            <span>
              <button
                type="button"
                className="btn ghost"
                disabled={busy}
                onClick={() => set({ group_roles: [...form.group_roles, { group: '', role_id: form.default_role_id }] })}
              >
                <Icon name="plus" size={14} /> Add mapping
              </button>
            </span>
            <div className="sso-pair">
              <div className="field">
                <span className="field-label">Default role</span>
                <Select label="Default role" value={form.default_role_id} options={roleOptions(form.default_role_id)} disabled={busy} onSelect={(v) => set({ default_role_id: v })} />
                <span className="field-hint">For a new person whose groups match nothing. People who already have an account keep their role.</span>
              </div>
              <Field label="Groups claim" hint="The field in the sign-in token that lists a person's groups.">
                <TextInput className="mono" value={form.groups_claim} disabled={busy} spellCheck={false} onChange={(e) => set({ groups_claim: e.target.value })} />
              </Field>
            </div>
            <hr className="sso-rule" />
            <div className="sso-require">
              <Toggle
                checked={form.require_sso}
                label="Require single sign-on"
                // Turning on needs a qualifying sign-in; turning off never does.
                disabled={busy || (!saved.require_sso && blockers.length > 0)}
                onChange={(v) => set({ require_sso: v })}
              />
              <div className="sso-sub">
                <h4>Require single sign-on</h4>
                <p>Passwords are refused for people, and no reset links are sent. Service accounts are unaffected.</p>
              </div>
            </div>
            {saved.require_sso ? (
              <div className="sso-conditions">
                <span className="sso-cond">
                  <Icon name="info" size={13} />
                  <span>
                    The discovery URL, client ID and the On switch are locked while this is on. If the provider is down, an
                    operator turns it off on the host: <code>python -m core.auth.require_sso_off</code>
                  </span>
                </span>
              </div>
            ) : (
              <div className="sso-conditions">
                {blockers.length === 0 ? (
                  <span className="sso-cond ok">
                    <Icon name="check" size={13} /> <span>You signed in through SSO with these settings.</span>
                  </span>
                ) : (
                  blockers.map((b) => (
                    <span key={b} className="sso-cond warn">
                      <Icon name="alert" size={13} /> <span>{b}.</span>
                    </span>
                  ))
                )}
              </div>
            )}
          </>
        )}

        <div className="sso-foot">
          <span className="text-xs text-tx-3">
            {reached
              ? 'A change to the discovery URL, client ID or secret is checked against the provider before it is saved.'
              : 'Saving checks that Vigil can reach the provider. Group mapping appears once it does.'}
          </span>
          <span className="grow" />
          {dirty && (
            <button className="btn ghost" disabled={busy} onClick={() => { setForm(toForm(saved)); setReplacing(false); setProblem(null) }}>
              Discard
            </button>
          )}
          <button className="btn primary" disabled={busy || !dirty || !valid} onClick={save}>
            {busy ? 'Checking…' : reached ? 'Save changes' : 'Save and check'}
          </button>
        </div>
      </div>
    </SettingsCard>
  )
}
