/* ============================================================
   Settings · System — platform metadata DB proxy (PgBouncer / SSH
   tunnel). Restart-required. Mirrors PlatformDatabaseTab.tsx.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { Field, PasswordInput, Select, SettingsCard, TextInput, ToggleRow } from '../../shared/ui'
import { usePlatformDatabase, type ProxyType } from './useSettings'
import type { SectionProps } from './types'

const PROXY_OPTIONS = [
  { value: 'none', label: 'None (direct connection)' },
  { value: 'pgbouncer', label: 'PgBouncer' },
  { value: 'ssh_tunnel', label: 'SSH tunnel' },
]

export default function SystemSection({ notify }: SectionProps) {
  const { form, setForm, hasPassword, hasPassphrase, phase, error, reload, save } =
    usePlatformDatabase()
  const [saving, setSaving] = useState(false)

  if (phase === 'loading') {
    return <div className="text-sm text-tx-3 py-16 text-center">Loading system settings…</div>
  }
  if (phase === 'error') {
    return (
      <div className="py-16 text-center flex flex-col items-center gap-2.5">
        <span className="text-sm text-tx-3">Couldn’t load platform DB proxy config: {error}</span>
        <button className="btn ghost" onClick={reload}>Retry</button>
      </div>
    )
  }

  const isSsh = form.proxy_type === 'ssh_tunnel'

  const handleSave = async () => {
    setSaving(true)
    try {
      await save(form)
      notify('ok', 'Platform DB proxy saved. Restart the backend for changes to take effect.')
    } catch (e) {
      notify('err', (e as { message?: string })?.message || 'Failed to save platform DB proxy.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <SettingsCard
      title="Platform Database Proxy"
      desc="Optional hop in front of Vigil's own metadata Postgres (PgBouncer pooler or SSH tunnel to a bastion). Credentials are kept in the encrypted secrets store. Changes take effect after a backend restart."
      actions={
        <button className="btn primary" onClick={handleSave} disabled={saving}>
          <Icon name="check2" /> {saving ? 'Saving…' : 'Save Configuration'}
        </button>
      }
    >
      <div className="flex flex-col gap-4 max-w-[520px]">
        <Field label="Proxy Type">
          <Select
            value={form.proxy_type}
            options={PROXY_OPTIONS}
            onSelect={(v) => setForm({ ...form, proxy_type: v as ProxyType })}
          />
        </Field>

        {form.proxy_type !== 'none' && (
          <>
            <Field label="Proxy Host">
              <TextInput
                value={form.proxy_host}
                placeholder={isSsh ? 'bastion.internal' : 'pgbouncer.internal'}
                onChange={(e) => setForm({ ...form, proxy_host: e.target.value })}
              />
            </Field>
            <Field label="Proxy Port">
              <TextInput
                type="number"
                value={form.proxy_port || ''}
                placeholder={isSsh ? '22' : '6432'}
                onChange={(e) =>
                  setForm({ ...form, proxy_port: parseInt(e.target.value, 10) || 0 })
                }
              />
            </Field>
            <Field label={isSsh ? 'SSH Username' : 'Proxy Username'}>
              <TextInput
                value={form.proxy_username}
                onChange={(e) => setForm({ ...form, proxy_username: e.target.value })}
              />
            </Field>
            <Field
              label="Proxy Password"
              hint={
                hasPassword
                  ? 'A password is stored. Leave blank to keep it; type a new value to overwrite.'
                  : 'Stored in the encrypted secrets store.'
              }
            >
              <PasswordInput
                value={form.proxy_password}
                onChange={(e) => setForm({ ...form, proxy_password: e.target.value })}
              />
            </Field>

            {isSsh && (
              <>
                <Field label="SSH Private Key Path" hint="Absolute path on the backend host.">
                  <TextInput
                    value={form.ssh_private_key_path}
                    placeholder="/home/vigil/.ssh/id_ed25519"
                    onChange={(e) => setForm({ ...form, ssh_private_key_path: e.target.value })}
                  />
                </Field>
                <Field
                  label="SSH Key Passphrase"
                  hint={
                    hasPassphrase
                      ? 'A passphrase is stored. Leave blank to keep it.'
                      : 'Required only with an encrypted private key.'
                  }
                >
                  <PasswordInput
                    value={form.ssh_key_passphrase}
                    onChange={(e) => setForm({ ...form, ssh_key_passphrase: e.target.value })}
                  />
                </Field>
              </>
            )}

            <ToggleRow
              label="Verify proxy TLS"
              checked={form.verify_proxy_tls}
              onChange={(v) => setForm({ ...form, verify_proxy_tls: v })}
            />
          </>
        )}
      </div>
    </SettingsCard>
  )
}
