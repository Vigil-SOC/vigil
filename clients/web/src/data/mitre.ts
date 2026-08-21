/* ============================================================
   MITRE ATT&CK technique metadata (name + tactic). Findings and
   the ATT&CK rollup carry technique IDs but the backend leaves
   `tactic`/`technique_name` unresolved ("Unknown" / == id), so we
   resolve them here. Ported/extended from services/mitre_lookup.py
   to cover the techniques in our dataset; lookups fall back to the
   base technique (T1059.001 → T1059) then to the id / em-dash.
   ============================================================ */
const DASH = '—'

interface Meta {
  name: string
  tactic: string
}

const TECHNIQUE_META: Record<string, Meta> = {
  T1003: { name: 'OS Credential Dumping', tactic: 'Credential Access' },
  'T1003.001': { name: 'LSASS Memory', tactic: 'Credential Access' },
  'T1003.002': { name: 'Security Account Manager', tactic: 'Credential Access' },
  T1021: { name: 'Remote Services', tactic: 'Lateral Movement' },
  'T1021.001': { name: 'Remote Desktop Protocol', tactic: 'Lateral Movement' },
  'T1021.002': { name: 'SMB / Windows Admin Shares', tactic: 'Lateral Movement' },
  T1027: { name: 'Obfuscated Files or Information', tactic: 'Defense Evasion' },
  T1041: { name: 'Exfiltration Over C2 Channel', tactic: 'Exfiltration' },
  T1047: { name: 'Windows Management Instrumentation', tactic: 'Execution' },
  T1053: { name: 'Scheduled Task/Job', tactic: 'Execution' },
  'T1053.005': { name: 'Scheduled Task', tactic: 'Execution' },
  T1055: { name: 'Process Injection', tactic: 'Defense Evasion' },
  'T1055.001': { name: 'DLL Injection', tactic: 'Defense Evasion' },
  'T1055.012': { name: 'Process Hollowing', tactic: 'Defense Evasion' },
  T1059: { name: 'Command and Scripting Interpreter', tactic: 'Execution' },
  'T1059.001': { name: 'PowerShell', tactic: 'Execution' },
  'T1059.003': { name: 'Windows Command Shell', tactic: 'Execution' },
  T1070: { name: 'Indicator Removal', tactic: 'Defense Evasion' },
  'T1070.004': { name: 'File Deletion', tactic: 'Defense Evasion' },
  T1071: { name: 'Application Layer Protocol', tactic: 'Command and Control' },
  'T1071.001': { name: 'Web Protocols', tactic: 'Command and Control' },
  'T1071.004': { name: 'DNS', tactic: 'Command and Control' },
  T1078: { name: 'Valid Accounts', tactic: 'Initial Access' },
  'T1078.001': { name: 'Default Accounts', tactic: 'Initial Access' },
  'T1078.002': { name: 'Domain Accounts', tactic: 'Initial Access' },
  T1110: { name: 'Brute Force', tactic: 'Credential Access' },
  'T1110.003': { name: 'Password Spraying', tactic: 'Credential Access' },
  T1190: { name: 'Exploit Public-Facing Application', tactic: 'Initial Access' },
  T1486: { name: 'Data Encrypted for Impact', tactic: 'Impact' },
  T1490: { name: 'Inhibit System Recovery', tactic: 'Impact' },
  T1547: { name: 'Boot or Logon Autostart Execution', tactic: 'Persistence' },
  'T1547.001': { name: 'Registry Run Keys / Startup Folder', tactic: 'Persistence' },
  T1566: { name: 'Phishing', tactic: 'Initial Access' },
  'T1566.001': { name: 'Spearphishing Attachment', tactic: 'Initial Access' },
  'T1566.002': { name: 'Spearphishing Link', tactic: 'Initial Access' },
  T1567: { name: 'Exfiltration Over Web Service', tactic: 'Exfiltration' },
  'T1567.002': { name: 'Exfiltration to Cloud Storage', tactic: 'Exfiltration' },
  T1574: { name: 'Hijack Execution Flow', tactic: 'Persistence' },
  'T1574.001': { name: 'DLL Search-Order Hijacking', tactic: 'Persistence' },
  T1018: { name: 'Remote System Discovery', tactic: 'Discovery' },
}

function lookup(id: string): Meta | undefined {
  if (!id || id === DASH) return undefined
  return TECHNIQUE_META[id] || TECHNIQUE_META[id.split('.')[0]]
}

/** human technique name; falls back to the technique id itself */
export function techniqueName(id: string): string {
  return lookup(id)?.name || id || DASH
}

/** MITRE tactic for a technique; em-dash when unknown */
export function techniqueTactic(id: string): string {
  return lookup(id)?.tactic || DASH
}
