import { describe, expect, it } from 'vitest'
import { catalogFrom, serverLabel } from './toolCatalog'

describe('serverLabel', () => {
  it('says vendors the way people write them', () => {
    expect(serverLabel('azure-ad')).toBe('Microsoft Entra ID (Azure AD)')
    expect(serverLabel('crowdstrike')).toBe('CrowdStrike')
    expect(serverLabel('palo-alto')).toBe('Palo Alto NGFW')
    expect(serverLabel('m365')).toBe('Microsoft 365')
    expect(serverLabel('aws')).toBe('AWS')
    expect(serverLabel('gcp-iam')).toBe('GCP IAM')
    expect(serverLabel('example-edr')).toBe('Example EDR')
  })

  it('files an integration tool under its label, not its server key', () => {
    const [t] = catalogFrom({ catalog: [{ name: 'azure-ad_get_user', server: 'azure-ad', group: 'azure-ad' }] })
    expect(t.group).toBe('Microsoft Entra ID (Azure AD)')
    const [v] = catalogFrom({ catalog: [{ name: 'list_cases', server: 'vigil', group: 'Cases' }] })
    expect(v.group).toBe('Cases')
  })
})
