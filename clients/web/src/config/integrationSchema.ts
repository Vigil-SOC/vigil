// UI-agnostic, so the console wizard and config/integrations.ts can share it.
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
// whose metadata has ``proxy_supported: true``. Applied by the integration's
// httpx clients via core/integrations/_base/proxy.py.
export const PROXY_FIELDS: IntegrationField[] = [
  {
    name: 'proxy_type',
    label: 'Proxy Type',
    type: 'select',
    section: 'proxy',
    default: 'none',
    options: [
      { value: 'none', label: 'None (direct connection)' },
      { value: 'http', label: 'HTTP proxy' },
      { value: 'socks5', label: 'SOCKS5 proxy' },
    ],
    helpText:
      'Route this integration\'s HTTP requests through a proxy. Leave as "None" for a direct connection.',
  },
  {
    name: 'proxy_host',
    label: 'Proxy Host',
    type: 'text',
    section: 'proxy',
    placeholder: 'e.g. proxy.internal',
    helpText: 'Hostname or IP of the proxy.',
  },
  {
    name: 'proxy_port',
    label: 'Proxy Port',
    type: 'number',
    section: 'proxy',
    placeholder: 'e.g. 3128, 1080',
  },
  {
    name: 'proxy_username',
    label: 'Proxy Username',
    type: 'text',
    section: 'proxy',
    helpText:
      'Auth user for the proxy. Leave blank if none.',
  },
  {
    name: 'proxy_password',
    label: 'Proxy Password',
    type: 'password',
    section: 'proxy',
    helpText:
      'Stored in the encrypted secrets store. Leave blank to keep the existing value.',
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
  // Appended to the proxy type help text when the proxy covers only part of
  // the integration (e.g. one of its servers).
  proxy_note?: string
}

export const fieldsWithProxy = (i: IntegrationMetadata): IntegrationField[] =>
  !i.proxy_supported
    ? i.fields
    : [
        ...i.fields,
        ...PROXY_FIELDS.map((f) =>
          f.name === 'proxy_type' && i.proxy_note
            ? { ...f, helpText: `${f.helpText} ${i.proxy_note}` }
            : f,
        ),
      ]
