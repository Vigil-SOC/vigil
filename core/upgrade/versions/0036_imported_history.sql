-- An import of a customer's pre-install alerts and closed tickets (#3965) writes
-- Sightings and Verdicts under investigation_kind 'imported', keyed by
-- '<source system>:<content hash>', and its marker is the import's only record.
-- Gaps and Distil failures are left alone: an import writes neither.
ALTER TABLE public.episodic_sightings
    DROP CONSTRAINT episodic_sightings_investigation_kind_check,
    ADD CONSTRAINT episodic_sightings_investigation_kind_check
        CHECK (investigation_kind IN ('hunt', 'case', 'analyst', 'imported'));

ALTER TABLE public.episodic_verdicts
    DROP CONSTRAINT episodic_verdicts_investigation_kind_check,
    ADD CONSTRAINT episodic_verdicts_investigation_kind_check
        CHECK (investigation_kind IN ('hunt', 'case', 'analyst', 'imported'));

ALTER TABLE public.episodic_distil_markers
    DROP CONSTRAINT episodic_distil_markers_investigation_kind_check,
    ADD CONSTRAINT episodic_distil_markers_investigation_kind_check
        CHECK (investigation_kind IN ('hunt', 'case', 'analyst', 'imported'));
