CREATE INDEX IF NOT EXISTS idx_finding_created_at ON public.findings USING btree (created_at DESC, finding_id DESC);
CREATE INDEX IF NOT EXISTS idx_intake_triggers_created_at ON public.intake_triggers USING btree (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intake_triggers_finding_id_id ON public.intake_triggers USING btree (finding_id, id DESC) WHERE (finding_id IS NOT NULL);
