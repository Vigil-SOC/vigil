-- The catch-up loop and the two admin conditions read cases by the two indicator
-- columns (#4734, epic #4580); the bell runs its counts on every request, so each
-- is a partial index. Plain CREATE INDEX on a table of Cases takes a moment, not a
-- rewrite; no row is read or changed.

-- Stale Cases, oldest first (the loop) and past the lag limit (the bell).
CREATE INDEX IF NOT EXISTS idx_case_indicators_stale
    ON public.cases (indicators_stale_since)
    WHERE indicators_stale_since IS NOT NULL;

-- Cases never reconciled: the backfill and its bell line. Shrinks to nothing.
CREATE INDEX IF NOT EXISTS idx_case_indicators_unsynced
    ON public.cases (case_id)
    WHERE indicators_synced_at IS NULL;

-- The drift sweep: the Cases reconciled longest ago.
CREATE INDEX IF NOT EXISTS idx_case_indicators_synced
    ON public.cases (indicators_synced_at)
    WHERE indicators_synced_at IS NOT NULL;
