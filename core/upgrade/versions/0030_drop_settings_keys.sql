-- settings.read and settings.write gate nothing (#3747). Remove both from every
-- role: built-in, legacy and custom. No successor is copied; 0008 did that.
UPDATE public.roles
SET permissions = permissions - 'settings.read' - 'settings.write'
WHERE permissions ? 'settings.read' OR permissions ? 'settings.write';
