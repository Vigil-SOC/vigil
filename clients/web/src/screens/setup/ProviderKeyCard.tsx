import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { urlIsLoopback } from '../../shared/loopback'
import { PasswordInput, SettingsCard, TextInput } from '../../shared/ui'
import { isMasked, secretText, type BifrostKey } from '../../services/bifrostApi'
import { CLOUD_PROVIDERS } from './providerResidency'
import CheckMark from './CheckMark'
import type { Outcome } from './providerOutcome'

export function OutcomeBanner({ outcome }: { outcome: Outcome }) {
  const good = outcome.tone === 'good'
  return (
    <div className={`su-banner${good ? '' : ' poor'}`} role={good ? 'status' : 'alert'}>
      {good ? <CheckMark phase="passed" /> : <Icon name="alert" size={18} />}
      <span>{outcome.text}</span>
    </div>
  )
}

const PICK_LABEL = { bedrock: 'AWS Bedrock', vertex: 'Google Vertex', azure: 'Azure' }

/**
 * The key card under the provider cards. Anthropic/OpenAI take an inline key,
 * Ollama a server URL, and a cloud account opens the key dialog (Vertex needs
 * service-account JSON). With a key already held and nothing typed, "Test again"
 * only re-checks it; the stored secret is never rewritten.
 */
export default function ProviderKeyCard({
  kind,
  provider,
  name,
  existing,
  outcome,
  busy,
  onTest,
  onPickCloud,
  onOpenDialog,
}: {
  kind: 'key' | 'cloud' | 'local'
  provider: string
  /** "Anthropic", "OpenAI" — for the placeholder */
  name: string
  existing: BifrostKey | undefined
  outcome: Outcome | undefined
  busy: boolean
  /** the typed key or server URL; '' retests what is stored */
  onTest: (value: string) => Promise<void>
  onPickCloud: (provider: string) => void
  onOpenDialog: () => void
}) {
  const storedUrl = secretText(existing?.ollama_key_config?.url)
  const [value, setValue] = useState(kind === 'local' && !isMasked(storedUrl) ? storedUrl : '')
  const label = busy ? 'Testing…' : outcome ? 'Test again' : kind === 'local' ? 'Test server' : 'Test key'
  const submit = async () => {
    if (busy) return
    await onTest(value.trim())
    if (kind === 'key') setValue('') // the secret is never kept on screen
  }

  return (
    <SettingsCard title={kind === 'cloud' ? 'Cloud account' : kind === 'local' ? 'Ollama server' : 'API key'}>
      <div className="flex flex-col gap-3">
        {kind === 'cloud' ? (
          <div className="su-actions">
            <div className="inline-flex gap-1.5" role="group" aria-label="Cloud provider">
              {CLOUD_PROVIDERS.map((p) => (
                <button
                  key={p}
                  type="button"
                  aria-pressed={provider === p}
                  className={`btn ${provider === p ? 'primary' : 'ghost'}`}
                  onClick={() => onPickCloud(p)}
                >
                  {PICK_LABEL[p]}
                </button>
              ))}
            </div>
            <button type="button" className="btn ghost test" onClick={onOpenDialog}>
              <Icon name="plus" size={14} />
              {existing ? 'Edit key' : 'Add key'}
            </button>
            {existing && (
              <button type="button" className="btn ghost" disabled={busy} onClick={submit}>
                <Icon name="bolt" size={14} />
                {busy ? 'Testing…' : 'Test again'}
              </button>
            )}
          </div>
        ) : (
          <div className="su-keyrow">
            <div className="grow min-w-0">
              {kind === 'local' ? (
                <TextInput
                  aria-label="Ollama server URL"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && submit()}
                  placeholder="env.OLLAMA_URL"
                  autoComplete="off"
                  spellCheck={false}
                />
              ) : (
                <PasswordInput
                  aria-label="API key"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && submit()}
                  placeholder={existing ? '•••••••• (unchanged)' : `Paste ${/^[AEIOU]/.test(name) ? 'an' : 'a'} ${name} API key`}
                  autoComplete="new-password"
                />
              )}
            </div>
            <button
              type="button"
              className="btn ghost test"
              disabled={busy || (kind === 'key' && !value.trim() && !existing)}
              onClick={submit}
            >
              <Icon name="bolt" size={14} />
              {label}
            </button>
          </div>
        )}
        <p className="su-note">
          {kind === 'local'
            ? 'Vigil talks to Ollama over your network. No data leaves this site. Leave blank to use the address the deployment already set.'
            : kind === 'cloud'
              ? 'Credentials are stored encrypted in the model gateway and never shown again. Vertex takes a service-account JSON.'
              : 'Stored encrypted in the model gateway and never shown again.'}
        </p>
        {kind === 'local' && urlIsLoopback(value) && (
          <p className="su-note warn">
            The gateway resolves this, and it runs in a container, so loopback is the container itself, not this
            machine. Ollama on the host is <code>http://host.docker.internal:11434</code>.
          </p>
        )}
        {outcome && <OutcomeBanner outcome={outcome} />}
      </div>
    </SettingsCard>
  )
}
