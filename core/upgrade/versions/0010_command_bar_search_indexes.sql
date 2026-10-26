CREATE INDEX IF NOT EXISTS idx_case_title_trgm ON public.cases USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_case_description_trgm ON public.cases USING gin (description gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_case_comment_content_trgm ON public.case_comments USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_case_evidence_name_trgm ON public.case_evidence USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_case_evidence_description_trgm ON public.case_evidence USING gin (description gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_case_ioc_value_trgm ON public.case_iocs USING gin (value gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_finding_finding_id_trgm ON public.findings USING gin (finding_id gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_finding_entity_context_trgm ON public.findings USING gin ((entity_context::text) gin_trgm_ops);
