/* ============================================================
   Settings · AI Config · Models

   The gateway's model catalogue, browsable. Rates and capabilities come from
   Bifrost's synced pricing datasheet rather than a list maintained here, so a
   model Bifrost can price is a model this page can describe — including ones
   released after this build.
   ============================================================ */
import { useState } from 'react'
import { Icon } from '../../shared/icons'
import { EmptyState, Field, SettingsCard, TextInput } from '../../shared/ui'
import { useBifrostModels, useModelParameters } from './useBifrost'
import { perMillion, type BifrostModel } from '../../../services/bifrostApi'

const money = (v: number | null): string =>
  v === null ? '—' : v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(3)}`

const CAPABILITIES: [keyof BifrostCapabilityFlags, string][] = [
  ['supports_function_calling', 'Tools'],
  ['supports_reasoning', 'Reasoning'],
  ['supports_prompt_caching', 'Prompt caching'],
  ['supports_vision', 'Vision'],
  ['supports_web_search', 'Web search'],
]

type BifrostCapabilityFlags = {
  supports_function_calling?: boolean
  supports_reasoning?: boolean
  supports_prompt_caching?: boolean
  supports_vision?: boolean
  supports_web_search?: boolean
}

export default function AiModelsPanel() {
  const [query, setQuery] = useState('')
  const { models, total, phase, error } = useBifrostModels(query)
  const [selected, setSelected] = useState<BifrostModel | null>(null)

  return (
    <SettingsCard
      wide
      title="Model Catalogue"
      desc="Every model the gateway can route, with the rates it bills against. Selecting one shows what Bifrost knows about it — this is the same pricing that lands on the cost dashboard."
    >
      <div className="max-w-[420px] mb-4">
        <Field label="Search" hint={phase === 'ready' ? `${total} models known to the gateway.` : ''}>
          <TextInput
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="claude, gpt, llama…"
          />
        </Field>
      </div>

      {phase === 'loading' && <EmptyState loading compact icon="sparkle" title="Loading catalogue…" />}
      {phase === 'error' && (
        <EmptyState error compact icon="alert" title="Couldn’t load the model catalogue" body={error} />
      )}
      {phase === 'ready' && models.length === 0 && (
        <EmptyState compact icon="search" title="No models match" body="Try a shorter search, or check that the provider holding them has a key." />
      )}

      {phase === 'ready' && models.length > 0 && (
        <div className="flex gap-4" style={{ alignItems: 'flex-start' }}>
          <div className="table-wrap grow">
            <table className="tbl">
              <thead>
                <tr><th>Model</th><th>Provider</th><th>Context</th></tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr
                    key={`${m.provider}/${m.name}`}
                    onClick={() => setSelected(m)}
                    style={{
                      cursor: 'pointer',
                      background:
                        selected?.name === m.name && selected?.provider === m.provider
                          ? 'var(--bg-3)'
                          : undefined,
                    }}
                  >
                    <td className="font-mono text-xs">{m.name}</td>
                    <td><span className="chip">{m.provider}</span></td>
                    <td className="text-xs">
                      {m.max_input_tokens ? `${Math.round(m.max_input_tokens / 1000)}k` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {selected && <ModelDetail model={selected} onClose={() => setSelected(null)} />}
        </div>
      )}
    </SettingsCard>
  )
}

function ModelDetail({ model, onClose }: { model: BifrostModel; onClose: () => void }) {
  const { params, error } = useModelParameters(model.name, model.provider)

  return (
    <div style={{ width: 300, flexShrink: 0, border: '1px solid var(--line)', borderRadius: 6, padding: 14 }}>
      <div className="flex items-start gap-2 mb-3">
        <div className="grow">
          <div className="font-mono text-xs break-all">{model.name}</div>
          <div className="text-xs text-tx-3">{model.provider}</div>
        </div>
        <button className="btn ghost icon" title="Close" onClick={onClose}>
          <Icon name="close" size={14} />
        </button>
      </div>

      {error && <div className="text-xs text-tx-3">{error}</div>}
      {!params && !error && <div className="text-xs text-tx-3">Loading…</div>}

      {params && (
        <>
          <div className="text-xs" style={{ letterSpacing: '0.04em', marginBottom: 6 }}>
            PER MILLION TOKENS
          </div>
          <div className="flex flex-col gap-1 text-sm mb-3">
            <Row label="Input" value={money(perMillion(params.input_cost_per_token))} />
            <Row label="Output" value={money(perMillion(params.output_cost_per_token))} />
            <Row label="Cache read" value={money(perMillion(params.cache_read_input_token_cost))} />
            <Row label="Cache write" value={money(perMillion(params.cache_creation_input_token_cost))} />
          </div>

          <div className="flex flex-col gap-1 text-sm mb-3">
            <Row
              label="Context"
              value={params.max_input_tokens ? `${params.max_input_tokens.toLocaleString()} in` : '—'}
            />
            <Row
              label="Max output"
              value={params.max_output_tokens ? params.max_output_tokens.toLocaleString() : '—'}
            />
          </div>

          <div className="flex flex-wrap gap-1.5">
            {CAPABILITIES.filter(([k]) => params[k]).map(([k, label]) => (
              <span key={k} className="chip">{label}</span>
            ))}
          </div>

          {params.deprecation_date && (
            <div className="text-xs mt-3" style={{ color: 'var(--high)' }}>
              <Icon name="clock" size={12} /> Retires {params.deprecation_date}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-tx-3">{label}</span>
      <span className="font-mono text-xs">{value}</span>
    </div>
  )
}
