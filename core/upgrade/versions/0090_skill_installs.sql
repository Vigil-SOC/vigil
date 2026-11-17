-- Where each operator skill installed from a git repo came from, one row per
-- skill name. core/skills/skill_sources.py writes a row on install and drops it
-- when the skill is deleted; a skill with no row was built, uploaded or copied in.
-- repo_url is the https URL cloned and label how it is shown (owner/repo);
-- ref is the branch or tag asked for (null: the default branch), path the
-- folder within the repo that was searched, and folder the skill's own folder.
-- commit_sha is the commit installed, so a newer one upstream can be told; digest
-- is the skill's content as installed, so an edit made in Vigil can be told.
-- Times are naive UTC like the rest of the schema (core/time.py).

CREATE TABLE IF NOT EXISTS public.skill_installs (
    skill_name TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    label TEXT NOT NULL,
    ref TEXT,
    path TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    digest TEXT NOT NULL,
    installed_by TEXT,
    installed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE INDEX IF NOT EXISTS idx_skill_installs_repo
    ON public.skill_installs (repo_url, ref, path);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.skill_installs TO vigil_app;
