-- Teams own cases (#4324). One default team, every active person on it, every
-- case placed there. The grants are literals: a later catalog edit must not
-- change what this revision writes. The trail entry is written by the revision
-- module, not here.

CREATE TABLE public.teams (
    team_id character varying(50) NOT NULL,
    name text NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    archived_at timestamp without time zone,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT teams_pkey PRIMARY KEY (team_id)
);

CREATE UNIQUE INDEX uq_teams_name ON public.teams USING btree (lower(name));
CREATE UNIQUE INDEX uq_teams_is_default ON public.teams USING btree (is_default)
    WHERE is_default;

INSERT INTO public.teams (team_id, name, is_default)
VALUES ('team-default', 'Default team', true);

CREATE TABLE public.team_memberships (
    team_id character varying(50) NOT NULL,
    principal_id text NOT NULL,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    CONSTRAINT team_memberships_pkey PRIMARY KEY (team_id, principal_id),
    CONSTRAINT team_memberships_team_id_fkey FOREIGN KEY (team_id)
        REFERENCES public.teams (team_id) ON DELETE RESTRICT,
    CONSTRAINT team_memberships_principal_id_fkey FOREIGN KEY (principal_id)
        REFERENCES public.principals (principal_id) ON DELETE RESTRICT
);

CREATE INDEX idx_team_memberships_principal
    ON public.team_memberships USING btree (principal_id);

-- A membership names a person. The upgrade's own insert is held to the same rule.
CREATE FUNCTION public.team_membership_person() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.principals
        WHERE principal_id = NEW.principal_id
          AND kind = 'person'
          AND deleted_at IS NULL
    ) THEN
        RAISE EXCEPTION 'team membership is for a person principal only';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER team_memberships_person
    BEFORE INSERT OR UPDATE ON public.team_memberships
    FOR EACH ROW EXECUTE FUNCTION public.team_membership_person();

INSERT INTO public.team_memberships (team_id, principal_id)
SELECT 'team-default', u.principal_id
FROM public.users u
JOIN public.principals p ON p.principal_id = u.principal_id
WHERE u.is_active
  AND NOT u.service_account
  AND p.kind = 'person'
  AND p.deleted_at IS NULL;

ALTER TABLE public.cases ADD COLUMN team_id character varying(50);

UPDATE public.cases
SET team_id = (SELECT team_id FROM public.teams WHERE is_default)
WHERE team_id IS NULL;

ALTER TABLE public.cases ALTER COLUMN team_id SET NOT NULL;

ALTER TABLE public.cases
    ADD CONSTRAINT cases_team_id_fkey FOREIGN KEY (team_id)
        REFERENCES public.teams (team_id) ON DELETE RESTRICT;

CREATE INDEX idx_case_team_id ON public.cases USING btree (team_id);

-- Teams chosen on an invite, kept until the account is created. Null means
-- the account joins the default team.
ALTER TABLE public.auth_links
    ADD COLUMN team_ids character varying(50)[];

-- teams.manage: Admin and Manager. cases.move: Admin, Manager, Senior Analyst.
UPDATE public.roles
SET permissions = permissions || '{"teams.manage": true}'::jsonb,
    updated_at = NOW()
WHERE role_id IN ('role-admin', 'role-manager');

UPDATE public.roles
SET permissions = permissions || '{"cases.move": true}'::jsonb,
    updated_at = NOW()
WHERE role_id IN ('role-admin', 'role-manager', 'role-senior-analyst');

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.teams TO vigil_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.team_memberships TO vigil_app;
GRANT EXECUTE ON FUNCTION public.team_membership_person() TO vigil_app;
