// enrichment / output / identity / sandbox / forensics are not telemetry
export const DATA_SOURCE_CATEGORIES = new Set<string>([
  'SIEM',
  'EDR/XDR',
  'Cloud Security',
  'Network Security',
  'Data Pipeline',
])
