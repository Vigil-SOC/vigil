-- 16_llm_budgets.sql
-- LLM budget enforcement via a Bifrost virtual key (#186).
--
-- This migration is intentionally minimal: a single global VK MVP per the
-- locked design decision. Vigil has no tenant model (per-user RBAC only),
-- so all calls share one Bifrost VK and one budget. The `virtual_key_id`
-- column is unused.

-- NOTE: the `virtual_key_id` column and `idx_llm_interaction_vk` index on
-- `llm_interaction_logs` are declared on the ORM model
-- (`database/models.py` `LLMInteractionLog`) and materialized by
-- SQLAlchemy's create_all() at backend startup — not here. See
-- `15_federation.sql` for the long explanation of why init-time ALTER
-- against an ORM-owned table breaks docker-compose's first-boot.

-- Seed the single bifrost.virtual_keys settings row. Empty default_vk
-- means "no enforcement yet" — Bifrost will accept calls without
-- x-bf-vk while we're in the bootstrap window. The Settings → LLM
-- Providers → Budgets sub-panel writes the configured VK ID here once
-- the operator provisions one in the Bifrost UI.
INSERT INTO system_config (key, value, description, config_type)
VALUES (
    'bifrost.virtual_keys',
    '{"default_vk": ""}'::jsonb,
    'Bifrost virtual-key configuration and budget settings (#186)',
    'ai'
)
ON CONFLICT (key) DO NOTHING;
