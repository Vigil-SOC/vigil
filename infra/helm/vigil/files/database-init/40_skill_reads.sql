-- One row per read of a skill body through the read_skill tool (#1560).
--
-- Skills live on disk (34_drop_skills.sql retired their table); this is a
-- usage log, not skill storage coming back. It exists so the console can say
-- "Used N times in 7 days · by M agents" per skill from actual reads: every
-- agent offered read_skill is offered the whole library, so a per-skill read
-- count is the only signal an operator gets about which skills are dead.
--
-- A row is written by core/skills/skill_usage.py from the one place a read
-- is known to have succeeded: /internal/tools/invoke, after the result has
-- been checked for an error. Only a read of the skill body counts -- a
-- follow-up read of a supporting file is the same use, not another one.
--
-- agent_id is nullable on purpose. Runs with no Vigil agent behind them
-- (hunt, investigate, rootcause, a chat with no agent) record NULL rather
-- than an invented id; the agents-per-skill count ignores NULLs.
--
-- Retention is 8 days, pruned by a delete on write in the recorder: the
-- console window is 7 days, and the extra day keeps a read from expiring
-- mid-window. No scheduler is involved.

CREATE TABLE IF NOT EXISTS skill_reads (
    id         bigserial   PRIMARY KEY,
    skill_name text        NOT NULL,
    agent_id   text,
    read_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE skill_reads IS
    'One row per successful read of a skill body via read_skill (#1560). A usage log; skills themselves live on disk. Rows are kept 8 days, pruned on write.';

COMMENT ON COLUMN skill_reads.agent_id IS
    'The Vigil agent that read the skill. NULL when no agent is behind the run (hunt, investigate, rootcause, plain chat); never an invented id.';

-- The console's one query: reads and distinct agents per skill over a
-- window, grouped by skill.
CREATE INDEX IF NOT EXISTS idx_skill_reads_skill_time
    ON skill_reads (skill_name, read_at);
