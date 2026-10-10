-- The graph builder is retired (epic #1615): nothing reads or writes a saved
-- canvas layout. Safe to re-run, and safe when the table or column is gone.
ALTER TABLE IF EXISTS custom_workflows DROP COLUMN IF EXISTS graph_layout;
