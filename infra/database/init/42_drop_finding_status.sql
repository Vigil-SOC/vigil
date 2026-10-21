-- A finding's state is derived from its dismissal and the case that holds it
-- (#2594): nothing reads or writes findings.status. Safe to re-run, and safe
-- when the table or column is gone.
DROP INDEX IF EXISTS idx_finding_status;
ALTER TABLE IF EXISTS findings DROP COLUMN IF EXISTS status;
