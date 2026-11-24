import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../../contexts/AuthContext'
import api, {
  directorySyncApi,
  ssoApi,
  type DirectorySync,
  type SsoSettings,
  type SsoSettingsBody,
} from '../../services/api'
import { errorText } from '../../shared/errorText'
import { utcDay } from '../../shared/utc'
import { Icon } from '../../shared/icons'
import { ConfirmDialog, Field, PasswordInput, Select, SettingsCard, TextInput, Toggle } from '../../shared/ui'
import DirectoryMapping from './DirectoryMapping'
import type { Role } from './useSettings'
import { DIRECTORY_PUSHES_ID, type SectionProps } from './types'

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

/** The save refuses an empty secret when one is stored and both fields are
 * filled with a discovery URL or client ID different from the saved values. */
const identityNeedsSecret = (saved: SsoSettings, discovery: string, clientId: string) =>
  !!saved.client_secret_set &&
  discovery.trim() !== '' &&
  clientId.trim() !== '' &&
  (discovery.trim() !== (saved.discovery_url ?? '').trim() ||
    clientId.trim() !== (saved.client_id ?? '').trim())

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
  const { hasPermission } = useAuth()
  const canSync =
    hasPermission('roles.manage') && hasPermission('users.write') && hasPermission('teams.manage')
  const [sync, setSync] = useState<DirectorySync | null>(null)
  const [claim, setClaim] = useState('sub')
  const [confirmOn, setConfirmOn] = useState(false)
  const [syncBusy, setSyncBusy] = useState(false)
  const [minted, setMinted] = useState<string | null>(null)

  const loadSync = useCallback(() => {
    directorySyncApi
      .get()
      .then((res) => {
        setSync(res.data)
        setClaim(res.data.directory_id_claim || 'sub')
      })
      .catch(() => setSync(null))
  }, [])

  const load = useCallback(() => {
    setFailed(null)
    ssoApi.get()
      .then((res) => { setSaved(res.data); setForm(toForm(res.data)); setReplacing(false); setProblem(null) })
      .catch((e) => setFailed(errorText(e, 'Could not load the single sign-on settings.')))
    loadSync()
    // Every role, custom ones too; without users.read the selects show only the ids saved.
    api.get('/users/roles/list').then((res) => setRoles(res.data?.roles ?? [])).catch(() => setRoles([]))
  }, [loadSync])
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
  // Entering from Replace drops a secret typed for the saved registration.
  // Leaving only when both fields match the saved values again drops one typed
  // for the other registration. A secret typed while the prompt is up stays,
  // including across a cleared field, and is what the save sends.
  const setIdentity = (patch: Partial<Pick<Form, 'discovery_url' | 'client_id'>>) => {
    const next = { ...form, ...patch }
    const was = identityNeedsSecret(saved, form.discovery_url, form.client_id)
    const now = identityNeedsSecret(saved, next.discovery_url, next.client_id)
    const matchesSaved =
      next.discovery_url.trim() === (saved.discovery_url ?? '').trim() &&
      next.client_id.trim() === (saved.client_id ?? '').trim()
    if (!was && now) {
      if (replacing || !form.client_secret) next.client_secret = ''
      setReplacing(false)
    } else if (was && !now && matchesSaved) {
      next.client_secret = ''
      setReplacing(false)
    }
    setForm(next)
  }
  const setRow = (i: number, patch: Partial<Mapping>) =>
    set({ group_roles: form.group_roles.map((g, j) => (j === i ? { ...g, ...patch } : g)) })
  const move = (i: number, by: number) => {
    const rows = [...form.group_roles]
    ;[rows[i], rows[i + by]] = [rows[i + by], rows[i]]
    set({ group_roles: rows })
  }

  const reached = !!saved.issuer
  const askForSecret = identityNeedsSecret(saved, form.discovery_url, form.client_id)
  // While required, the provider and SSO itself stay as saved: turn Require off first.
  const locked = !!saved.require_sso
  const syncOn = !!(sync?.sync_enabled || saved.sync_enabled)
  // Directory sync keeps the provider it was turned on against.
  const providerLocked = locked || syncOn
  const lockedTitle = locked ? 'Turn off Require single sign-on first' : undefined
  const providerLockTitle = syncOn
    ? 'Directory sync is on. Turn it off on the host before changing the provider or turning single sign-on off.'
    : lockedTitle
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

  const pushSync = async (mint: boolean) => {
    setSyncBusy(true)
    setProblem(null)
    try {
      const res = await directorySyncApi.save(claim.trim() || 'sub', mint)
      setSync(res.data)
      setClaim(res.data.directory_id_claim || 'sub')
      if (res.data.token) setMinted(res.data.token)
      setConfirmOn(false)
      notify('ok', res.data.sync_enabled ? 'Directory sync is on.' : 'Directory sync saved.')
    } catch (e) {
      setProblem(errorText(e, 'Directory sync was not changed.'))
    } finally {
      setSyncBusy(false)
    }
  }

  const when = (value: string | null) =>
    value ? new Date(value).toLocaleString() : 'None yet'

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
            title={providerLockTitle ?? (reached ? undefined : 'Save settings that reach the provider first')}
            disabled={busy || providerLocked || (!reached && !form.enabled)}
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
            disabled={busy || providerLocked}
            title={providerLockTitle}
            spellCheck={false}
            onChange={(e) => setIdentity({ discovery_url: e.target.value })}
          />
          {reached && (
            <span className="sso-reached">
              <Icon name="check" size={12} /> Reached when last saved. Issuer: {saved.issuer}
            </span>
          )}
        </div>
        <div className="sso-pair">
          <Field label="Client ID">
            <TextInput className="mono" value={form.client_id} disabled={busy || providerLocked} title={providerLockTitle} spellCheck={false} onChange={(e) => setIdentity({ client_id: e.target.value })} />
          </Field>
          {askForSecret ? (
            <Field label="Client secret">
              <PasswordInput
                value={form.client_secret}
                disabled={busy}
                required
                aria-required
                autoComplete="new-password"
                onChange={(e) => set({ client_secret: e.target.value })}
              />
            </Field>
          ) : saved.client_secret_set && !replacing && !form.client_secret ? (
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

        <hr className="sso-rule" />
        <div className="sso-sub">
          <h4>Two-step sign-in</h4>
          <p>
            Require MFA and Require phishing-resistant MFA apply to SSO sign-ins too. Vigil reads the provider's signed ID
            token: <code>amr</code> containing <code>mfa</code> meets Require MFA, and <code>acr</code> of <code>phr</code> or{' '}
            <code>phrh</code> meets both. Each counts only with an <code>auth_time</code> inside the sign-in length. Otherwise
            Vigil asks for its own factor, or has the person set one up.
          </p>
          <p>
            Vigil asks for <code>amr</code>, <code>acr</code> and <code>auth_time</code> at every sign-in. If your provider
            doesn't send them, add them to this client's ID token there. A provider's own labels don't count.
          </p>
          <p>
            The claims mean what your provider's configuration makes them mean. Providers document "remember this device",
            but not what the claims carry on such a sign-in, so Vigil can't tell when the provider skipped its second factor.
          </p>
        </div>

        {reached && (
          <>
            <hr className="sso-rule" />
            <div className="sso-sub">
              <h4>{syncOn ? 'Groups' : 'Roles from groups'}</h4>
              <p>
                {syncOn
                  ? 'Pushed groups set the role and the teams of a managed account. Nothing is applied until this mapping is saved.'
                  : 'Checked top to bottom at each sign-in. The first match wins.'}
              </p>
            </div>
            {syncOn ? (
              <DirectoryMapping
                canSync={canSync}
                roles={roles}
                settingsVersion={saved.version}
                notify={notify}
                onApplied={load}
              />
            ) : (
            <>
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
            </>
            )}
            <div className="sso-pair">
              <div className="field">
                <span className="field-label">Default role</span>
                <Select label="Default role" value={form.default_role_id} options={roleOptions(form.default_role_id)} disabled={busy} onSelect={(v) => set({ default_role_id: v })} />
                <span className="field-hint">
                  {syncOn
                    ? 'For a managed account whose groups match nothing. Applied when the mapping is saved or a group changes.'
                    : 'For a new person whose groups match nothing. People who already have an account keep their role.'}
                </span>
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
                <p>
                  Passwords are refused for people, and no reset links are sent. A sign-in already open that didn’t come
                  through SSO ends no later than its next refresh or Confirm it’s you. Service accounts and break-glass
                  sign-ins are unaffected.
                </p>
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

        <hr className="sso-rule" />
        <div className="sso-sub">
          <h4>Directory sync</h4>
          <p>
            The provider pushes people here. New people sign in only after a push, and a managed account stops taking its
            name, email and role from sign-in. Microsoft Entra ID and Okta are unverified until an operator runs them.
          </p>
        </div>
        {minted && (
          <div className="mcp-callout" role="alert">
            <strong>Copy this token now. It is shown once and cannot be recovered.</strong>
            <pre className="mcp-token">{minted}</pre>
            <button type="button" className="btn" onClick={() => setMinted(null)}>
              I have copied it
            </button>
          </div>
        )}
        <Field
          label="Directory id claim"
          hint="The sign-in value the provider also sends as externalId. Microsoft Entra's sign-in subject is per application, so that install sets this to oid and sends the same value as externalId."
        >
          <TextInput
            className="mono"
            value={claim}
            disabled={syncBusy}
            spellCheck={false}
            aria-label="Directory id claim"
            onChange={(e) => setClaim(e.target.value)}
          />
        </Field>
        {!syncOn && sync?.first_reminder_at && (
          <p className="text-xs text-tx-3">
            Sync is off, so its token is under the unused-token rule: if it is not used, its first reminder comes on{' '}
            {utcDay(sync.first_reminder_at)} UTC.
          </p>
        )}
        {syncOn && sync && (
          <dl className="sso-status">
            <div>
              <dt>Sync</dt>
              <dd>On</dd>
            </div>
            <div>
              <dt>Last push</dt>
              <dd>{when(sync.last_push)}</dd>
            </div>
            <div>
              <dt>Token expires</dt>
              <dd>{sync.token_expires_at ? when(sync.token_expires_at) : 'No token'}</dd>
            </div>
            <div>
              <dt>Failed authentications</dt>
              <dd>{sync.failed_authentications}</dd>
            </div>
            <div>
              <dt>Skipped joins</dt>
              <dd>{sync.skipped_joins}</dd>
            </div>
            <div>
              <dt>Waiting for a link</dt>
              <dd>
                {!sync.pending_links ? (
                  'None'
                ) : hasPermission('users.write') ? (
                  // the list is further down this screen, on Team & access
                  <a
                    className="underline"
                    href={`#${DIRECTORY_PUSHES_ID}`}
                    onClick={(e) => {
                      e.preventDefault()
                      document.getElementById(DIRECTORY_PUSHES_ID)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                    }}
                  >
                    {pushes(sync.pending_links)}
                  </a>
                ) : (
                  pushes(sync.pending_links)
                )}
              </dd>
            </div>
            <div>
              <dt>Endpoint</dt>
              <dd className="font-mono">{sync.scim_path}</dd>
            </div>
          </dl>
        )}
        {syncOn && sync && (
          <p className="text-xs text-tx-3">
            While sync is on, its token is exempt from the unused-token rule. Its maximum lifetime still applies: it
            expires on the date above.
          </p>
        )}
        {syncOn && sync?.mapping_note && (
          <div className="settings-banner warn" role="status">
            <Icon name="alert" size={14} />
            <span>{sync.mapping_note}</span>
          </div>
        )}
        {syncOn && sync && sync.broken_lines.length > 0 && (
          <div className="settings-banner warn" role="status">
            <Icon name="alert" size={14} />
            <span>
              {sync.broken_lines
                .map((line) =>
                  line.on === 'team'
                    ? `A team line names a group that is gone (${line.external_id}).`
                    : `A role line names a group that is gone (${line.external_id}).`,
                )
                .join(' ')}
            </span>
          </div>
        )}
        {syncOn && sync && sync.last_manager_refusals.length > 0 && (
          <div className="settings-banner warn" role="status">
            <Icon name="alert" size={14} />
            <span>
              A push could not deactivate the last person who can manage roles, and they are
              still active: {sync.last_manager_refusals.join(', ')}. Deactivate them in Team &amp;
              access once someone else can manage roles.
            </span>
          </div>
        )}
        {syncOn && sync && sync.role_refusals.length > 0 && (
          <div className="settings-banner warn" role="status">
            <Icon name="alert" size={14} />
            <span>
              A group change left the role in place for the last person who can manage roles:{' '}
              {sync.role_refusals.join(', ')}. Save the group mapping again once someone else can
              manage roles.
            </span>
          </div>
        )}
        <p className="text-xs text-tx-3">
          The provider has to reach this API at {sync?.scim_path ?? '/scim/v2'}. An install nothing can reach disables
          leavers by hand. Turn sync off on the host with{' '}
          <code>python -m core.auth.directory_sync_off</code>.
        </p>
        <span className="sso-line">
          {!syncOn ? (
            <button
              type="button"
              className="btn primary"
              disabled={syncBusy || !canSync || !saved.enabled}
              title={
                !saved.enabled
                  ? 'Turn single sign-on on first'
                  : !canSync
                    ? 'Needs roles.manage, users.write and teams.manage'
                    : undefined
              }
              onClick={() => setConfirmOn(true)}
            >
              Turn on directory sync
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn"
                disabled={syncBusy || !canSync || claim.trim() === (sync?.directory_id_claim ?? '')}
                onClick={() => void pushSync(false)}
              >
                Save claim
              </button>
              <button
                type="button"
                className="btn"
                disabled={syncBusy || !canSync}
                onClick={() => void pushSync(true)}
              >
                Mint a new token
              </button>
            </>
          )}
        </span>

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
      <ConfirmDialog
        open={confirmOn}
        title="Turn on directory sync?"
        danger={false}
        busy={syncBusy}
        confirmLabel="Turn on"
        onClose={() => !syncBusy && setConfirmOn(false)}
        onConfirm={() => void pushSync(false)}
        width={520}
        body={
          <>
            <p>
              New people sign in only after a push. Managed accounts stop taking a name, email and role from sign-in.
              The claim must be the same value the provider sends as externalId.
            </p>
            <p className="mt-2">
              Microsoft Entra's sign-in subject is per application, so that install sets the claim to oid and sends
              the same value as externalId.
            </p>
            <p className="mt-2">
              While sync is on, its token is exempt from the unused-token rule, so an unused token is not reminded or
              revoked. The token's maximum lifetime still applies.
            </p>
          </>
        }
      />
    </SettingsCard>
  )
}

const pushes = (n: number) => `${n} push${n === 1 ? ' waits' : 'es wait'} for a link`
