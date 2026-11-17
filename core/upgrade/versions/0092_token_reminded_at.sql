-- When a token's unused-token reminder went out (#4725). Null: none yet. The
-- sweep in services/api/token_sweep.py sets it; core/auth/token_unused.py judges
-- whether it still counts. Naive UTC like the rest of the schema (core/time.py).

ALTER TABLE public.mcp_credentials
    ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMP WITHOUT TIME ZONE;
