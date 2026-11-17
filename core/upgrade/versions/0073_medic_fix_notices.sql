-- Fix notices for holders of medic.admin (#4421).
-- New event names so a fix row is not also a #4338 opened, cleared or summary.
-- The watch in core/platform/medic_incidents.py is the only writer.

ALTER TABLE public.medic_incident_notice
    DROP CONSTRAINT IF EXISTS medic_incident_notice_event;

ALTER TABLE public.medic_incident_notice
    ADD CONSTRAINT medic_incident_notice_event
    CHECK (
        event IN (
            'opened',
            'cleared',
            'summary',
            'fix_open',
            'fix_not_held',
            'fix_not_back',
            'fix_fixed'
        )
    );
