-- API tokens (#4223): the token that replaced this one when it was rotated.
-- Startup's create_all may already have built the column from the model.

ALTER TABLE public.mcp_credentials ADD COLUMN IF NOT EXISTS successor_id character varying(50);
