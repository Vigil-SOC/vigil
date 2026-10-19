## Your environment (surveyed by code before your first turn)

Splunk index lab: 4 sourcetypes.

- edr:process: 1,200 events, 2026-05-05 06:00 → 2026-05-05 18:00
  fields: event_id, host, user, logon_id, process_guid, parent_guid, image, cmdline, sha256
- edr:file: 300 events, 2026-05-05 06:00 → 2026-05-05 18:00
  fields: event_id, host, process_guid, file_name, sha256, action
- auth:logon: 90 events, 2026-05-05 06:00 → 2026-05-05 18:00
  fields: logon_id, user, sid, workstation, logon_type, src_ip
- mail:gateway: 40 events, 2026-05-05 06:00 → 2026-05-05 18:00
  fields: msg_id, sender, recipient, subject, attachment_name, attachment_sha256, verdict
