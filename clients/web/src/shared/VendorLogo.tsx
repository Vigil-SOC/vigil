// One small mark per vendor, wherever a vendor is named. A Simple Icons mark when
// one exists, otherwise a lettered badge in the vendor's colour; null for words
// that are not a vendor, so the caller keeps its own icon.
import { ICON_PATHS } from './vendorIcons'

type Mark = { kind: 'svg'; title: string; hex: string; d: string } | { kind: 'letters'; title: string; hex: string; text: string }

// First match wins, so the more specific names come first.
const VENDORS: [RegExp, string | { title: string; hex: string; text: string }][] = [
  [/crowd ?strike|falcon/, { title: 'CrowdStrike', hex: '#E01F3D', text: 'CS' }],
  [/palo ?alto|pan-?os|ngfw/, 'paloaltonetworks'],
  [/splunk/, 'splunk'],
  [/service ?now|snow\b/, { title: 'ServiceNow', hex: '#1D8A5B', text: 'SN' }],
  [/jira/, 'jira'],
  [/atlassian/, 'atlassian'],
  [/slack/, 'slack'],
  [/okta/, 'okta'],
  [/entra|azure ?ad/, { title: 'Microsoft Entra ID', hex: '#0078D4', text: 'EN' }],
  [/defender|m365|microsoft[ _]?365|office ?365/, { title: 'Microsoft Defender', hex: '#0078D4', text: 'DF' }],
  [/sentinel ?one|\bs1\b/, { title: 'SentinelOne', hex: '#6B2FBA', text: 'S1' }],
  [/sentinel/, { title: 'Microsoft Sentinel', hex: '#0078D4', text: 'SE' }],
  [/windows ?security|winsec/, { title: 'Windows Security', hex: '#0078D4', text: 'WS' }],
  [/sysmon/, { title: 'Sysmon', hex: '#0078D4', text: 'SY' }],
  [/wazuh/, { title: 'Wazuh', hex: '#3595F9', text: 'W' }],
  [/zeek/, { title: 'Zeek', hex: '#3E6FB0', text: 'Z' }],
  [/vstrike/, { title: 'vStrike', hex: '#7C3AED', text: 'vS' }],
  [/loglm|deep ?tempo/, { title: 'DeepTempo LogLM', hex: '#0EA5E9', text: 'DT' }],
  [/darktrace/, { title: 'Darktrace', hex: '#2B2D6E', text: 'DK' }],
  [/opencti/, { title: 'OpenCTI', hex: '#001BDA', text: 'CT' }],
  [/security ?hub|\baws\b|amazon/, { title: 'AWS', hex: '#E88B1A', text: 'AWS' }],
  [/elastic/, 'elastic'],
  [/opensearch/, { title: 'OpenSearch', hex: '#005EB8', text: 'OS' }],
  [/zscaler/, { title: 'Zscaler', hex: '#0090D0', text: 'ZS' }],
  [/proofpoint/, { title: 'Proofpoint', hex: '#2E3191', text: 'PP' }],
  [/infoblox/, { title: 'Infoblox', hex: '#00A3E0', text: 'IB' }],
  [/cloudflare/, { title: 'Cloudflare', hex: '#F38020', text: 'CF' }],
  [/virus ?total/, 'virustotal'],
  [/fortinet|fortigate/, 'fortinet'],
  [/cisco/, 'cisco'],
  [/qualys/, 'qualys'],
  [/kafka/, 'apachekafka'],
  [/github/, 'github'],
  [/google/, 'google'],
  [/nemotron|nvidia/, 'nvidia'],
  [/ollama/, 'ollama'],
  [/openai|gpt-/, 'openai'],
  [/anthropic|claude/, 'anthropic'],
]

// Marks drawn in black would vanish on the dark theme; those follow the text colour.
const INK = new Set(['#000000', '#181717', '#231F20', '#191919'])

export function vendorMark(name?: string | null): Mark | null {
  const key = (name || '').toLowerCase()
  if (!key) return null
  for (const [re, v] of VENDORS) {
    if (!re.test(key)) continue
    if (typeof v !== 'string') return { kind: 'letters', ...v }
    const icon = ICON_PATHS[v]
    return icon ? { kind: 'svg', ...icon } : null
  }
  return null
}

export default function VendorLogo({ name, size = 16, title }: { name?: string | null; size?: number; title?: string }) {
  const mark = vendorMark(name)
  if (!mark) return null
  const label = title ?? mark.title
  if (mark.kind === 'svg') {
    const fill = INK.has(mark.hex.toUpperCase()) ? 'currentColor' : mark.hex
    return (
      <svg className="vendor-logo" width={size} height={size} viewBox="0 0 24 24" role="img" aria-label={label} style={{ flex: 'none' }}>
        <path d={mark.d} fill={fill} />
      </svg>
    )
  }
  return (
    <span
      className="vendor-logo"
      role="img"
      aria-label={label}
      title={label}
      style={{
        flex: 'none',
        display: 'inline-grid',
        placeItems: 'center',
        width: size,
        height: size,
        borderRadius: Math.round(size * 0.25),
        background: mark.hex,
        color: '#fff',
        fontSize: Math.max(7, Math.round(size * (mark.text.length > 2 ? 0.36 : 0.48))),
        fontWeight: 700,
        letterSpacing: '-0.02em',
        lineHeight: 1,
        fontFamily: 'inherit',
      }}
    >
      {mark.text}
    </span>
  )
}
