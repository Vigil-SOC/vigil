import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { WatchersCard } from './CaseSections'
import { casesApi } from '../../services/api'

/**
 * The watchers card read `added_at`, but the API sends `created_at` — so
 * `fmtD` was always called with `undefined` and every row rendered
 * "Watching since —". It failed quietly: `fmtD` guards falsy input, and the
 * `?` on the interface field kept TypeScript happy, so nothing surfaced the
 * mismatch. Fixed in #584; this pins it. See #561.
 */

/* `fmtD` formats in local time, so an unpinned TZ makes the expected date
   depend on where the suite runs (UTC-10 rolls 09:14Z back a day). */
vi.stubEnv('TZ', 'UTC')

vi.mock('../../services/api', () => ({
  casesApi: {
    getWatchers: vi.fn(),
    addWatcher: vi.fn(),
    removeWatcher: vi.fn(),
  },
}))

/** The shape `CaseWatcherSchema` dumps (core/storage/schemas/case_entities.py). */
const WATCHER_ROW = {
  case_id: 'case-2026-0142',
  user_id: 'analyst@example.com',
  notification_preferences: {},
  created_at: '2026-06-15T09:14:00Z',
}

beforeEach(() => {
  vi.mocked(casesApi.getWatchers).mockResolvedValue({
    data: { watchers: [WATCHER_ROW] },
  } as never)
})

describe('WatchersCard', () => {
  it('renders the date the watcher started watching', async () => {
    render(<WatchersCard caseId="case-2026-0142" />)

    await waitFor(() =>
      expect(screen.getByText('analyst@example.com')).toBeInTheDocument(),
    )

    expect(screen.getByText(/Watching since Jun 15, 2026/)).toBeInTheDocument()
  })

  it('does not fall back to the em-dash placeholder for a real timestamp', async () => {
    // The precise pre-fix symptom. Asserting only the date above would still
    // pass if the component rendered both, so pin the absence too.
    render(<WatchersCard caseId="case-2026-0142" />)

    await waitFor(() =>
      expect(screen.getByText('analyst@example.com')).toBeInTheDocument(),
    )

    expect(screen.queryByText('Watching since —')).not.toBeInTheDocument()
  })

  it('still shows the placeholder when the API omits the timestamp', async () => {
    // `CaseWatcherSchema.created_at` is optional, so the field can be absent
    // from the payload — that path must keep degrading gracefully rather than
    // rendering "Invalid Date".
    vi.mocked(casesApi.getWatchers).mockResolvedValue({
      data: { watchers: [{ ...WATCHER_ROW, created_at: undefined }] },
    } as never)

    render(<WatchersCard caseId="case-2026-0142" />)

    await waitFor(() =>
      expect(screen.getByText('analyst@example.com')).toBeInTheDocument(),
    )

    expect(screen.getByText('Watching since —')).toBeInTheDocument()
  })
})
