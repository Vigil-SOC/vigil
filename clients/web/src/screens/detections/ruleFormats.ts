// The rule formats a person can name for a repo or a local source.
export const FORMAT_OPTIONS = [
  { value: 'sigma', label: 'Sigma (YAML)' },
  { value: 'splunk', label: 'Splunk ESCU (YAML)' },
  { value: 'elastic', label: 'Elastic (TOML)' },
  { value: 'kql', label: 'KQL (MD/YAML/KQL)' },
  { value: 'auto', label: 'Auto-detect' },
]
