import { useCallback, useEffect, useState } from 'react'
import { COMPONENT_LABELS } from '../../config/aiComponents'
import { aiConfigApi, type AIConfigResponse } from '../../services/api'
import { Icon } from '../../shared/icons'
import { Banner, extractApiError } from '../../shared/formKit'
import { SettingsCard } from '../../shared/ui'

type State = { phase: 'loading' } | { phase: 'error'; message: string } | { phase: 'ready'; config: AIConfigResponse }

/** "What Vigil will use": one read-only row per AI component, from the assignments Settings › AI models edits. */
export default function ModelPlan() {
  const [state, setState] = useState<State>({ phase: 'loading' })

  const load = useCallback(() => {
    setState({ phase: 'loading' })
    aiConfigApi
      .getConfig()
      .then(({ data }) => setState({ phase: 'ready', config: data }))
      .catch((e) => setState({ phase: 'error', message: extractApiError(e, 'Could not read the model assignments') }))
  }, [])
  useEffect(load, [load])

  let body
  if (state.phase === 'loading') {
    body = <p className="su-note">Loading what Vigil will use…</p>
  } else if (state.phase === 'error') {
    body = (
      <div className="flex flex-col gap-2">
        <Banner kind="err">{state.message}</Banner>
        <button type="button" className="btn ghost self-start" onClick={load}>
          <Icon name="refresh" size={14} /> Retry
        </button>
      </div>
    )
  } else {
    const { components = [], assignments = {} } = state.config
    body =
      components.length && Object.keys(assignments).length ? (
        <div className="su-plan" role="list">
          {components.map((id) => {
            const info = COMPONENT_LABELS[id]
            return (
              <div key={id} role="listitem" className="su-plan-row">
                <span className="su-plan-who">{info?.label ?? id}</span>
                <span className="su-plan-model">{assignments[id]?.model_id || 'Provider default'}</span>
                <span className="su-plan-why" title={info?.description}>
                  {info?.description}
                </span>
              </div>
            )
          })}
        </div>
      ) : (
        <p className="su-note">No model assigned yet</p>
      )
  }

  return (
    <SettingsCard title="What Vigil will use" desc="Change any of them in Settings › AI models.">
      {body}
    </SettingsCard>
  )
}
