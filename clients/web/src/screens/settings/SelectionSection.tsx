import { useEffect, useState, type JSX } from 'react'
import { day1Api } from '../../services/api'
import { useAuth } from '../../contexts/AuthContext'
import { errorText } from '../../shared/errorText'
import { Icon } from '../../shared/icons'
import { SettingsCard } from '../../shared/ui'
import type { SectionProps } from './types'
import {
  applyToggle,
  closureProblem,
  sameSelection,
  saveEffect,
  type Catalogue,
  type CatalogueKind,
  type SelectionDraft,
  type SelectionView,
} from './selectionForm'

const INACTIVE = "ticked, but its jurisdiction isn't selected: changes nothing"

const offered = (view: SelectionView): SelectionDraft => ({
  jurisdictions: view.jurisdictions.filter((id) => view.catalogue.jurisdictions.some((row) => row.id === id)),
  kinds: view.kinds.filter((id) => view.catalogue.kinds.some((row) => row.id === id)),
})

function join(items: string[]): string {
  if (items.length <= 1) return items[0] ?? ''
  if (items.length === 2) return `${items[0]} and ${items[1]}`
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

/** Settings › System › Jurisdictions & kinds (#5010). */
export default function SelectionSection({ notify }: SectionProps) {
  const { hasPermission } = useAuth()
  const canEdit = hasPermission('platform.write')
  const [view, setView] = useState<SelectionView | null>(null)
  const [draft, setDraft] = useState<SelectionDraft>({ jurisdictions: [], kinds: [] })
  const [failed, setFailed] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let live = true
    day1Api.selection()
      .then(({ data }) => {
        if (!live) return
        setView(data)
        setDraft(offered(data))
      })
      .catch((e) => { if (live) setFailed(errorText(e, 'Could not load the selection. Reload to try again.')) })
    return () => { live = false }
  }, [])

  const toggle = (axis: 'jurisdiction' | 'kind', id: string, on: boolean) => {
    if (!view) return
    setDraft(applyToggle(draft, view.catalogue, axis, id, on))
  }

  const save = async () => {
    if (!view) return
    setSaving(true)
    try {
      const { data } = await day1Api.setSelection(draft)
      setView(data)
      setDraft(offered(data))
      notify('ok', 'Saved what Vigil handles, and what your organisation is.')
    } catch (e) {
      notify('err', errorText(e, 'Could not save the selection.'))
    } finally {
      setSaving(false)
    }
  }

  if (failed) {
    return (
      <div className="settings-banner err">
        <Icon name="alert" size={14} />
        <span>{failed}</span>
      </div>
    )
  }
  if (!view) return <p className="text-sm text-tx-3">Loading…</p>

  const saved = offered(view)
  const problem = closureProblem(draft, view.catalogue)
  const edited = !sameSelection(draft, saved)
  // A save sends only offered ids, so it drops anything this release no longer offers.
  const dirty = edited || view.unoffered.length > 0
  const effect = saveEffect(saved, draft, view.catalogue)
  const effectText = [
    effect.brings.length ? `This save brings ${join(effect.brings)}.` : '',
    effect.clears.length ? `This save clears ${join(effect.clears)}.` : '',
  ].filter(Boolean).join(' ')

  return (
    <SettingsCard
      title="What Vigil handles, and what your organisation is"
      actions={canEdit && (
        <>
          {edited && (
            <button className="btn ghost" onClick={() => setDraft(saved)} disabled={saving}>Discard</button>
          )}
          <button className="btn primary" onClick={save} disabled={!dirty || !!problem || saving}>
            <Icon name="check" /> {saving ? 'Saving…' : 'Save'}
          </button>
        </>
      )}
    >
      <div className="sel-banners">
        {view.unoffered.length > 0 && (
          <div className="settings-banner warn">
            <Icon name="alert" size={14} />
            <span>
              No longer offered by this release:{' '}
              {view.unoffered.map((item) => `${item.axis} ${item.id}`).join(', ')}.
              {' '}Saving drops these.
            </span>
          </div>
        )}
        {problem && (
          <div className="settings-banner warn">
            <Icon name="alert" size={14} />
            <span>{problem}</span>
          </div>
        )}
        {effectText && (
          <div className="settings-banner info">
            <Icon name="info" size={14} />
            <span>{effectText}</span>
          </div>
        )}
      </div>
      <p className="sel-note">A jurisdiction is your statement of whose rules you are subject to.</p>
      <div className="sel-stack">
        {jurisdictionRows(view.catalogue, null, 0, draft, canEdit, toggle)}
      </div>
      <h4 className="sel-kicker">No jurisdiction named</h4>
      <div className="sel-stack">
        {view.catalogue.kinds.filter((kind) => kind.jurisdiction === null).map((kind) => (
          <KindRow
            key={kind.id}
            kind={kind}
            depth={0}
            checked={draft.kinds.includes(kind.id)}
            inactive={false}
            canEdit={canEdit}
            onChange={(on) => toggle('kind', kind.id, on)}
          />
        ))}
      </div>
      {!canEdit && <p className="sel-note sel-foot">Only an administrator can change this.</p>}
    </SettingsCard>
  )
}

function jurisdictionRows(
  catalogue: Catalogue,
  parent: string | null,
  depth: number,
  draft: SelectionDraft,
  canEdit: boolean,
  toggle: (axis: 'jurisdiction' | 'kind', id: string, on: boolean) => void,
): JSX.Element[] {
  return catalogue.jurisdictions
    .filter((row) => row.parent === parent)
    .flatMap((row) => {
      const kinds = catalogue.kinds.filter((kind) => kind.jurisdiction === row.id)
      const note = row.sentence === 'New York'
        ? 'An entity exempt from Part 500 in any other way ticks neither New York kind.'
        : null
      return [
        <CheckRow
          key={row.id}
          depth={depth}
          checked={draft.jurisdictions.includes(row.id)}
          label={row.sentence}
          canEdit={canEdit}
          onChange={(on) => toggle('jurisdiction', row.id, on)}
        />,
        ...(note ? [<p key={`${row.id}-note`} className="sel-note-row" style={{ paddingLeft: 12 + (depth + 1) * 22 }}>{note}</p>] : []),
        ...kinds.map((kind) => (
          <KindRow
            key={kind.id}
            kind={kind}
            depth={depth + 1}
            checked={draft.kinds.includes(kind.id)}
            inactive={checkedInactive(draft, kind)}
            canEdit={canEdit}
            onChange={(on) => toggle('kind', kind.id, on)}
          />
        )),
        ...jurisdictionRows(catalogue, row.id, depth + 1, draft, canEdit, toggle),
      ]
    })
}

function checkedInactive(draft: SelectionDraft, kind: CatalogueKind): boolean {
  return draft.kinds.includes(kind.id) && !!kind.jurisdiction && !draft.jurisdictions.includes(kind.jurisdiction)
}

function KindRow({
  kind, depth, checked, inactive, canEdit, onChange,
}: {
  kind: CatalogueKind
  depth: number
  checked: boolean
  inactive: boolean
  canEdit: boolean
  onChange: (on: boolean) => void
}) {
  return (
    <CheckRow
      depth={depth}
      checked={checked}
      label={kind.sentence}
      flag={inactive ? INACTIVE : null}
      canEdit={canEdit}
      onChange={onChange}
    />
  )
}

function CheckRow({
  depth, checked, label, flag, canEdit, onChange,
}: {
  depth: number
  checked: boolean
  label: string
  flag?: string | null
  canEdit: boolean
  onChange: (on: boolean) => void
}) {
  return (
    <label className={`sel-row${canEdit ? '' : ' is-disabled'}`} style={{ paddingLeft: 12 + depth * 22 }}>
      <input
        type="checkbox"
        checked={checked}
        disabled={!canEdit}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="sel-row-text">
        <span className="sel-row-label">{label}</span>
        {flag && <span className="sel-row-flag">{flag}</span>}
      </span>
    </label>
  )
}
