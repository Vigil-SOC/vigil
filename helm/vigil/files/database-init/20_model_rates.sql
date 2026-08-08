-- One model rate table, read by both languages (GH #589, #593)
-- Rates currently live in the agent layer's configuration files and in
-- services/model_registry.py, and have already drifted once: a config shipped
-- rates three times too high, which would have tripped the budget guard at a
-- third of the real spend. This table is the single source both read.
--
-- Every rate is USD per MILLION tokens, here and everywhere. The Python
-- registry stores per-million in its catalog and hands out per-1k on
-- ModelInfo; that dual unit for one number is the drift this table ends.
--
-- Cache rates are stored explicitly rather than derived from a per-provider
-- multiplier, so the agent layer never reimplements get_cache_multipliers().
--
-- Seeding from the Python registry and deleting the redundant cost arithmetic
-- is GH #593; this file ships the schema only. Idempotent.

CREATE TABLE IF NOT EXISTS model_rates (
    model_id             text          NOT NULL,
    provider_type        text          NOT NULL,
    input_per_mtok       numeric(12,6) NOT NULL,
    output_per_mtok      numeric(12,6) NOT NULL,
    cache_read_per_mtok  numeric(12,6) NOT NULL,
    cache_write_per_mtok numeric(12,6) NOT NULL,
    pricing_source       text          NOT NULL
        CHECK (pricing_source IN ('exact', 'heuristic', 'zero', 'unknown')),
    updated_at           timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, provider_type)
);

COMMENT ON TABLE model_rates IS
    'Model pricing read by both the Python backend and the TypeScript agent '
    'layer. The budget gate prices against this in-loop; a missing row is a '
    'refusal, never a zero, so an unpriced model cannot disable the cost cap.';

COMMENT ON COLUMN model_rates.pricing_source IS
    'exact from a published price list, heuristic from a model-family guess, '
    'zero for a deliberately free model. The budget gate distinguishes them.';
