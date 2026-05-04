-- 16_llm_budgets.sql
-- Per-tenant LLM budget enforcement via Bifrost virtual keys (#186).
--
-- This migration is intentionally minimal: a single global VK MVP per the
-- locked design decision. Vigil has no tenant model today (per-user RBAC
-- only), so all calls share one Bifrost VK and one budget. The
-- `virtual_key_id` column lands now to future-proof per-tenant attribution
-- once tenancy ships (#165).

ALTER TABLE llm_interaction_logs
    ADD COLUMN IF NOT EXISTS virtual_key_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_llm_interaction_vk
    ON llm_interaction_logs (virtual_key_id, created_at);

-- Seed the single bifrost.virtual_keys settings row. Empty default_vk
-- means "no enforcement yet" — Bifrost will accept calls without
-- x-bf-vk while we're in the bootstrap window. The Settings → LLM
-- Providers → Budgets sub-panel writes the configured VK ID here once
-- the operator provisions one in the Bifrost UI.
INSERT INTO system_config (key, value, description, config_type)
VALUES (
    'bifrost.virtual_keys',
    '{
        "default_vk": "",
        "budget_limit_usd": 0,
        "enforcement_mode": "warning"
    }'::jsonb,
    'Bifrost virtual-key configuration and budget settings (#186)',
    'ai'
)
ON CONFLICT (key) DO NOTHING;
