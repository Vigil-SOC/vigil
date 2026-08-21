// Integration credential schema: the field shapes the wizard renders from.
// UI-agnostic so the console wizard and config/integrations.ts can share it.

export interface IntegrationField {
  name: string
  label: string
  type: 'text' | 'password' | 'url' | 'number' | 'boolean' | 'select'
  required?: boolean
  default?: any
  placeholder?: string
  helpText?: string
  options?: Array<{ value: string; label: string }>
  // Optional grouping. Fields without a section render in the main
  // body; fields sharing a non-default section render inside a
  // collapsible accordion with that section's label.
  section?: string
}

// Human label and default-collapsed state for known sections. Anything
// not listed renders with the section name as its title.
export const SECTION_LABELS: Record<string, string> = {
  proxy: 'Network / Proxy (optional)',
}

// Shared "Network / Proxy" field block. Appended to any integration
// whose metadata has ``proxy_supported: true``. Backend-side handling
// lives in services/db_proxy.py + services/integration_bridge_service.py.
export const PROXY_FIELDS: IntegrationField[] = [
  {
    name: 'proxy_type',
    label: 'Proxy Type',
    type: 'select',
    section: 'proxy',
    default: 'none',
    options: [
      { value: 'none', label: 'None (direct connection)' },
      { value: 'pgbouncer', label: 'PgBouncer (Postgres-only)' },
      { value: 'http', label: 'HTTP proxy' },
      { value: 'socks5', label: 'SOCKS5 proxy' },
      { value: 'ssh_tunnel', label: 'SSH tunnel' },
    ],
    helpText:
      'Route this integration through an intermediate hop. Leave as "None" for a direct connection.',
  },
  {
    name: 'proxy_host',
    label: 'Proxy Host',
    type: 'text',
    section: 'proxy',
    placeholder: 'e.g. bastion.internal',
    helpText: 'Hostname or IP of the proxy / pooler / bastion.',
  },
  {
    name: 'proxy_port',
    label: 'Proxy Port',
    type: 'number',
    section: 'proxy',
    placeholder: 'e.g. 6432, 1080, 22',
  },
  {
    name: 'proxy_username',
    label: 'Proxy Username',
    type: 'text',
    section: 'proxy',
    helpText:
      'Auth user for the proxy or SSH bastion. Leave blank if none.',
  },
  {
    name: 'proxy_password',
    label: 'Proxy Password',
    type: 'password',
    section: 'proxy',
    helpText:
      'Stored in the encrypted secrets store. Leave blank to keep the existing value.',
  },
  {
    name: 'ssh_private_key_path',
    label: 'SSH Private Key Path',
    type: 'text',
    section: 'proxy',
    placeholder: 'e.g. /home/vigil/.ssh/id_ed25519',
    helpText: 'Used only when Proxy Type is "SSH tunnel".',
  },
  {
    name: 'ssh_key_passphrase',
    label: 'SSH Key Passphrase',
    type: 'password',
    section: 'proxy',
    helpText:
      'Stored in the encrypted secrets store. Used only with an encrypted private key.',
  },
  {
    name: 'verify_proxy_tls',
    label: 'Verify Proxy TLS',
    type: 'boolean',
    section: 'proxy',
    default: true,
    helpText: 'Disable only when explicitly testing against a self-signed proxy.',
  },
]

export interface IntegrationMetadata {
  id: string
  name: string
  category: string
  description: string
  functionality_type?: string
  has_ui?: boolean
  icon?: string
  fields: IntegrationField[]
  docs_url?: string
  // When true, the wizard appends the shared PROXY_FIELDS block to
  // ``fields`` and renders it under the "Network / Proxy" section.
  proxy_supported?: boolean
}
