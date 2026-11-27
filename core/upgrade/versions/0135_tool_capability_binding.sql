-- The capability a person bound an outside tool to (#5312). NULL: the shipped
-- binding in core/integrations/bindings.yaml applies. {}: a person bound
-- nothing. {"capability": ..., "server": ...}: a person's binding.
ALTER TABLE public.tool_effect_classes
    ADD COLUMN IF NOT EXISTS binding JSONB;
