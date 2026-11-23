/** Close a selection from the catalogue the read returns. No second copy of the rules. */

export interface CatalogueJurisdiction {
  id: string
  sentence: string
  parent: string | null
}

export interface CatalogueKind {
  id: string
  sentence: string
  jurisdiction: string | null
  brings: string[]
  excludes: string[]
}

export interface Catalogue {
  jurisdictions: CatalogueJurisdiction[]
  kinds: CatalogueKind[]
}

export interface SelectionDraft {
  jurisdictions: string[]
  kinds: string[]
}

export interface UnofferedId {
  id: string
  axis: string
}

export interface SelectionView {
  jurisdictions: string[]
  kinds: string[]
  unoffered: UnofferedId[]
  inactive: { id: string; notice: string }[]
  catalogue: Catalogue
}

type Axis = 'jurisdiction' | 'kind'

function ancestors(catalogue: Catalogue, id: string): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  let parent = catalogue.jurisdictions.find((row) => row.id === id)?.parent ?? null
  while (parent && !seen.has(parent)) {
    seen.add(parent)
    out.push(parent)
    parent = catalogue.jurisdictions.find((row) => row.id === parent)?.parent ?? null
  }
  return out
}

function descendants(catalogue: Catalogue, id: string): Set<string> {
  const drop = new Set<string>([id])
  let grew = true
  while (grew) {
    grew = false
    for (const row of catalogue.jurisdictions) {
      if (row.parent && drop.has(row.parent) && !drop.has(row.id)) {
        drop.add(row.id)
        grew = true
      }
    }
  }
  return drop
}

function dropUnbrought(catalogue: Catalogue, kinds: Set<string>) {
  let changed = true
  while (changed) {
    changed = false
    for (const row of catalogue.kinds) {
      if (!kinds.has(row.id)) continue
      if (row.brings.some((id) => !kinds.has(id))) {
        kinds.delete(row.id)
        changed = true
      }
    }
  }
}

function ordered(catalogue: Catalogue, jurisdictions: Set<string>, kinds: Set<string>): SelectionDraft {
  return {
    jurisdictions: catalogue.jurisdictions.map((row) => row.id).filter((id) => jurisdictions.has(id)),
    kinds: catalogue.kinds.map((row) => row.id).filter((id) => kinds.has(id)),
  }
}

/** The set a toggle leaves, already closed. Jurisdictions follow a kind only when that kind asks. */
export function applyToggle(
  draft: SelectionDraft,
  catalogue: Catalogue,
  axis: Axis,
  id: string,
  on: boolean,
): SelectionDraft {
  const jurisdictions = new Set(draft.jurisdictions)
  const kinds = new Set(draft.kinds)
  if (axis === 'jurisdiction') {
    if (on) {
      jurisdictions.add(id)
      for (const parent of ancestors(catalogue, id)) jurisdictions.add(parent)
    } else {
      const drop = descendants(catalogue, id)
      for (const jurisdiction of drop) jurisdictions.delete(jurisdiction)
      for (const row of catalogue.kinds) {
        if (row.jurisdiction && drop.has(row.jurisdiction)) kinds.delete(row.id)
      }
    }
  } else if (axis === 'kind') {
    const kind = catalogue.kinds.find((row) => row.id === id)
    if (!kind) return draft
    if (on) {
      for (const excluded of kind.excludes) kinds.delete(excluded)
      kinds.add(kind.id)
      const queue = [...kind.brings]
      const brought = new Set<string>([kind.id])
      while (queue.length) {
        const next = queue.shift()
        if (!next || brought.has(next)) continue
        brought.add(next)
        kinds.add(next)
        const row = catalogue.kinds.find((item) => item.id === next)
        if (row) queue.push(...row.brings)
      }
      dropUnbrought(catalogue, kinds)
      for (const broughtId of brought) {
        if (!kinds.has(broughtId)) continue
        const row = catalogue.kinds.find((item) => item.id === broughtId)
        if (!row?.jurisdiction) continue
        jurisdictions.add(row.jurisdiction)
        for (const parent of ancestors(catalogue, row.jurisdiction)) jurisdictions.add(parent)
      }
    } else {
      kinds.delete(id)
      dropUnbrought(catalogue, kinds)
    }
  } else {
    const unreachable: never = axis
    return unreachable
  }
  return ordered(catalogue, jurisdictions, kinds)
}

/** Why a stored set cannot be saved yet, in the same order the write checks. */
export function closureProblem(draft: SelectionDraft, catalogue: Catalogue): string | null {
  const jurisdictions = new Set(draft.jurisdictions)
  const kinds = new Set(draft.kinds)
  for (const row of catalogue.jurisdictions) {
    if (!jurisdictions.has(row.id) || !row.parent || jurisdictions.has(row.parent)) continue
    const parent = catalogue.jurisdictions.find((item) => item.id === row.parent)
    return `${row.sentence} requires ${parent?.sentence ?? row.parent}`
  }
  for (const row of catalogue.kinds) {
    if (!kinds.has(row.id)) continue
    if (row.jurisdiction && !jurisdictions.has(row.jurisdiction)) {
      const place = catalogue.jurisdictions.find((item) => item.id === row.jurisdiction)
      return `${row.sentence} requires ${place?.sentence ?? row.jurisdiction}`
    }
    for (const broughtId of row.brings) {
      if (kinds.has(broughtId)) continue
      const brought = catalogue.kinds.find((item) => item.id === broughtId)
      return `${row.sentence} requires ${brought?.sentence ?? broughtId}`
    }
  }
  const reported = new Set<string>()
  for (const row of catalogue.kinds) {
    if (!kinds.has(row.id)) continue
    for (const otherId of row.excludes) {
      const pair = [row.id, otherId].sort().join('\0')
      if (!kinds.has(otherId) || reported.has(pair)) continue
      reported.add(pair)
      const other = catalogue.kinds.find((item) => item.id === otherId)
      return `${row.sentence} and ${other?.sentence ?? otherId} exclude each other`
    }
  }
  return null
}

function sentence(catalogue: Catalogue, axis: Axis, id: string): string {
  if (axis === 'jurisdiction') {
    return catalogue.jurisdictions.find((row) => row.id === id)?.sentence ?? id
  }
  if (axis === 'kind') {
    return catalogue.kinds.find((row) => row.id === id)?.sentence ?? id
  }
  const unreachable: never = axis
  return unreachable
}

function added(before: string[], after: string[]): string[] {
  const held = new Set(before)
  return after.filter((id) => !held.has(id))
}

export function saveEffect(
  saved: SelectionDraft,
  draft: SelectionDraft,
  catalogue: Catalogue,
): { brings: string[]; clears: string[] } {
  const brings = [
    ...added(saved.jurisdictions, draft.jurisdictions).map((id) => sentence(catalogue, 'jurisdiction', id)),
    ...added(saved.kinds, draft.kinds).map((id) => sentence(catalogue, 'kind', id)),
  ]
  const clears = [
    ...added(draft.jurisdictions, saved.jurisdictions).map((id) => sentence(catalogue, 'jurisdiction', id)),
    ...added(draft.kinds, saved.kinds).map((id) => sentence(catalogue, 'kind', id)),
  ]
  return { brings, clears }
}

export function sameSelection(a: SelectionDraft, b: SelectionDraft): boolean {
  const eq = (left: string[], right: string[]) =>
    left.length === right.length && left.every((id) => right.includes(id))
  return eq(a.jurisdictions, b.jurisdictions) && eq(a.kinds, b.kinds)
}
