import { PROXY_FIELDS, type IntegrationMetadata } from '../../config/integrationSchema'

export type ConnectConfig = Record<string, unknown>

export const fieldsOf = (i: IntegrationMetadata) =>
  i.proxy_supported ? [...i.fields, ...PROXY_FIELDS] : i.fields

/**
 * Required fields still empty. A stored secret satisfies its check (it comes back
 * redacted and is kept when left blank), so password fields key off `secretsSet`.
 */
export function missingFields(
  i: IntegrationMetadata,
  config: ConnectConfig,
  secretsSet: Record<string, boolean>,
) {
  return fieldsOf(i).filter((f) => {
    if (!f.required) return false
    const v = config[f.name] ?? f.default
    return (v === undefined || v === '') && !(f.type === 'password' && secretsSet[f.name] === true)
  })
}
