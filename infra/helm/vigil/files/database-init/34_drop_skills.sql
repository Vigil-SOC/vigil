-- The skills table and the run-start snapshot of skill tools are retired
-- (epic #882, #928): skills are SKILL.md directories read from disk, and no
-- caller ever filled skill_tools_available. Safe to re-run, and safe when
-- either relation is already gone.
DROP TABLE IF EXISTS skills;
ALTER TABLE IF EXISTS workflow_runs DROP COLUMN IF EXISTS skill_tools_available;
