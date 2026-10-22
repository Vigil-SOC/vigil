-- Principals, sign-ins, and credentials that outlive the account (#2758).
--
-- Startup's create_all may already have built principals and sign_ins from
-- the models, and it never adds users.principal_id or changes a foreign key,
-- so every step here tolerates either state.

CREATE TABLE IF NOT EXISTS public.principals (
    principal_id text NOT NULL,
    kind character varying(20) NOT NULL,
    label text,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    deleted_at timestamp without time zone,
    CONSTRAINT principals_pkey PRIMARY KEY (principal_id),
    CONSTRAINT principals_kind_check
        CHECK (kind IN ('person', 'service_account', 'agent', 'system'))
);

CREATE TABLE IF NOT EXISTS public.sign_ins (
    sign_in_id character varying(50) NOT NULL,
    principal_id text NOT NULL,
    started_at timestamp without time zone DEFAULT now() NOT NULL,
    expires_at timestamp without time zone,
    ended_at timestamp without time zone,
    end_reason character varying(20),
    CONSTRAINT sign_ins_pkey PRIMARY KEY (sign_in_id),
    CONSTRAINT sign_ins_end_reason_check
        CHECK (end_reason IN ('sign_out', 'expiry', 'revoked')),
    CONSTRAINT sign_ins_principal_id_fkey FOREIGN KEY (principal_id)
        REFERENCES public.principals (principal_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_sign_ins_principal
    ON public.sign_ins USING btree (principal_id);

-- One principal per account; its id is the user_id.
INSERT INTO public.principals (principal_id, kind, label)
SELECT user_id,
       CASE WHEN service_account THEN 'service_account' ELSE 'person' END,
       username
FROM public.users
ON CONFLICT (principal_id) DO NOTHING;

-- The system, each built-in agent (AgentId in core/agents/builtins.py), and
-- each agent-service plugin. Not orchestrator, and not the child-run roles.
INSERT INTO public.principals (principal_id, kind, label) VALUES
    ('system', 'system', 'the system'),
    ('agent:triage', 'agent', 'Triage agent'),
    ('agent:investigator', 'agent', 'Investigation agent'),
    ('agent:threat_hunter', 'agent', 'Threat hunter'),
    ('agent:correlator', 'agent', 'Correlation agent'),
    ('agent:responder', 'agent', 'Response agent'),
    ('agent:reporter', 'agent', 'Reporting agent'),
    ('agent:mitre_analyst', 'agent', 'MITRE ATT&CK analyst'),
    ('agent:forensics', 'agent', 'Forensics agent'),
    ('agent:threat_intel', 'agent', 'Threat intel agent'),
    ('agent:compliance', 'agent', 'Compliance agent'),
    ('agent:malware_analyst', 'agent', 'Malware analyst'),
    ('agent:network_analyst', 'agent', 'Network analyst'),
    ('agent:auto_responder', 'agent', 'Auto-response agent'),
    ('agent:hunt', 'agent', 'Hunt'),
    ('agent:investigate', 'agent', 'Investigate'),
    ('agent:rootcause', 'agent', 'Root cause'),
    ('agent:adjudicate', 'agent', 'Adjudicate'),
    ('agent:compose', 'agent', 'Compose'),
    ('agent:tally', 'agent', 'Tally'),
    ('agent:chat', 'agent', 'the chat assistant')
ON CONFLICT (principal_id) DO NOTHING;

INSERT INTO public.principals (principal_id, kind, label)
SELECT 'agent:' || id, 'agent', name
FROM public.custom_agents
ON CONFLICT (principal_id) DO NOTHING;

ALTER TABLE public.users ADD COLUMN IF NOT EXISTS principal_id text;
UPDATE public.users SET principal_id = user_id WHERE principal_id IS NULL;
ALTER TABLE public.users ALTER COLUMN principal_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass AND conname = 'users_principal_id_key'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_principal_id_key UNIQUE (principal_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.users'::regclass AND conname = 'users_principal_id_fkey'
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_principal_id_fkey FOREIGN KEY (principal_id)
            REFERENCES public.principals (principal_id) ON DELETE RESTRICT;
    END IF;
END $$;

-- Deleting a user revokes its credentials and keeps them, so the delete no
-- longer cascades to them.
ALTER TABLE public.mcp_credentials
    DROP CONSTRAINT IF EXISTS mcp_credentials_user_id_fkey,
    ADD CONSTRAINT mcp_credentials_user_id_fkey FOREIGN KEY (user_id)
        REFERENCES public.users (user_id) ON DELETE RESTRICT;
