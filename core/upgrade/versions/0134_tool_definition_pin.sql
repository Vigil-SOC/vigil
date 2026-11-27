-- The definition (name, description, input schema) a remote server's tool was
-- classified against, its hash, and a later definition that differs (#5300).
-- A non-null changed_definition withdraws the tool until a person classifies it
-- again. Existing rows are pinned by the registry to the first definition it
-- sees after this, as 'system'.
ALTER TABLE public.tool_effect_classes
    ADD COLUMN IF NOT EXISTS pinned_definition JSONB,
    ADD COLUMN IF NOT EXISTS pinned_hash TEXT,
    ADD COLUMN IF NOT EXISTS changed_definition JSONB;
