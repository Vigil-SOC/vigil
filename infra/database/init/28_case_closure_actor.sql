-- Which kind of actor closed a Case, recorded at the close.
--
-- `case_closure_info` is created by SQLAlchemy rather than declared here, so
-- this file only adds what episodic memory needs and cannot derive. That is one
-- column: closing a Case writes a Verdict (#733), and a Verdict's Trust says
-- whether a person or an agent concluded. `closed_by` cannot answer it -- an
-- agent closing as "soc-automation" and an analyst closing as "nestor" are the
-- same shape of string, and a lookup against users would grade a departed
-- analyst's close as an agent's.
--
-- It is asked at the write, where the answer is known for free: the HTTP close
-- has an authenticated principal behind it and the MCP tool does not.
--
-- Defaulting existing rows to 'agent' understates rather than overstates. Every
-- close that predates this column came through the MCP tool or the API with a
-- caller-supplied name, and grading an unknown closer an analyst would put the
-- system's highest-trust record on rows that never earned it.
ALTER TABLE case_closure_info
    ADD COLUMN IF NOT EXISTS closed_by_kind text NOT NULL DEFAULT 'agent';

ALTER TABLE case_closure_info
    DROP CONSTRAINT IF EXISTS case_closure_info_closed_by_kind_check;
ALTER TABLE case_closure_info
    ADD CONSTRAINT case_closure_info_closed_by_kind_check
        CHECK (closed_by_kind IN ('analyst', 'agent'));

COMMENT ON COLUMN case_closure_info.closed_by_kind IS
    'Whether a person or an agent closed this case; read by episodic memory as Trust.';
