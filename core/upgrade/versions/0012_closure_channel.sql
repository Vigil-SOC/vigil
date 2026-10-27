-- Trust follows the console sign-in (#3192). The channel is stored; the
-- analyst/agent word is derived. closed_by becomes a principal id only when
-- it is a live username. The previous text stays. Run ids, "system", and
-- anything else stay text. Old cases.activities JSON is left as it was.

ALTER TABLE public.case_closure_info
    ADD COLUMN IF NOT EXISTS close_channel character varying(16) DEFAULT 'agent'::character varying NOT NULL;

ALTER TABLE public.case_closure_info
    ADD COLUMN IF NOT EXISTS closed_by_text character varying(100);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'case_closure_info'
          AND column_name = 'closed_by_kind'
    ) THEN
        UPDATE public.case_closure_info
           SET closed_by_text = closed_by,
               close_channel = CASE
                   WHEN closed_by_kind = 'analyst' THEN 'cookie'
                   ELSE 'agent'
               END;

        UPDATE public.case_closure_info AS c
           SET closed_by = u.principal_id
          FROM public.users AS u
          JOIN public.principals AS p ON p.principal_id = u.principal_id
         WHERE c.closed_by = u.username
           AND p.deleted_at IS NULL;

        ALTER TABLE public.case_closure_info
            DROP CONSTRAINT IF EXISTS case_closure_info_closed_by_kind_check;
        ALTER TABLE public.case_closure_info
            DROP COLUMN closed_by_kind;
    END IF;
END $$;

ALTER TABLE public.case_closure_info
    DROP CONSTRAINT IF EXISTS case_closure_info_close_channel_check;

ALTER TABLE public.case_closure_info
    ADD CONSTRAINT case_closure_info_close_channel_check
    CHECK (close_channel IN ('cookie', 'agent'));

COMMENT ON COLUMN public.case_closure_info.close_channel IS
    'How the closer authenticated. cookie is a person at the console; Trust is derived from this.';
COMMENT ON COLUMN public.case_closure_info.closed_by_text IS
    'Actor text kept when closed_by is a principal id, or the same text when it was not a username.';
