import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { WatchersCard } from './CaseSections'
import { casesApi } from '../../../services/api'

/**
 * The watchers card read `added_at`, but `CaseWatcher.to_dict()` emits
 * `created_at` — so `fmtD` was always called with `undefined` and every row
 * rendered "Watching since —". It failed quietly: `fmtD` guards falsy input,
 * and the `?` on the interface field kept TypeScript happy, so nothing
 * surfaced the mismatch. See #561.
 */

vi.mock('../../../services/api', () => ({
  casesApi: {
    getWatchers: vi.fn(),
    addWatcher: vi.fn(),
    removeWatcher: vi.fn(),
  },
}))

/** Exactly the shape `CaseWatcher.to_dict()` returns (database/models.py). */
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
    // The column is nullable, so `created_at` can legitimately be absent —
    // that path must keep degrading gracefully rather than rendering
    // "Invalid Date".
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
