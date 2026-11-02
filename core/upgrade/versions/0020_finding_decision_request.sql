CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_trail_finding_decision ON public.audit_trail ((payload ->> 'finding_id'), (payload ->> 'request_id')) WHERE payload ->> 'action' = 'finding.decided';
