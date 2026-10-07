-- Second model a custom agent may use in chat (GH #1325).
-- Null means unset. Chat tries `model`, then `fallback_model`, on the
-- provider the component assignment already resolved.

ALTER TABLE custom_agents
    ADD COLUMN IF NOT EXISTS fallback_model TEXT;

COMMENT ON COLUMN custom_agents.fallback_model IS
    'Optional second model id for chat. Null = unset. Tried after model on the assignment provider.';
