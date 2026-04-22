-- Migration: Add AI enrichment field to findings table
-- This allows caching AI-generated analysis to avoid regenerating it

-- Add ai_enrichment column to findings table
ALTER TABLE findings 
ADD COLUMN IF NOT EXISTS ai_enrichment JSONB;

-- Create index for fast lookup of enriched vs non-enriched findings
CREATE INDEX IF NOT EXISTS idx_finding_has_enrichment 
ON findings ((ai_enrichment IS NOT NULL));

-- Add comment
COMMENT ON COLUMN findings.ai_enrichment IS 'AI-generated enrichment data cached on first view, includes threat summary, impact analysis, recommendations, and related techniques';

