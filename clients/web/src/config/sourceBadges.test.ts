import { describe, expect, it } from 'vitest'
import { CATALOG_TO_SOURCE, sourceBadge } from './sourceBadges'

describe('sourceBadge', () => {
  it.each([
    ['microsoft_defender', 'Defender'],
    ['azure_sentinel', 'Sentinel'],
    ['aws_security_hub', 'Security Hub'],
    ['darktrace', 'Darktrace'],
    ['cloudflare_cloudy', 'Cloudflare'],
    ['elastic', 'Elastic'],
    ['splunk', 'Splunk'],
  ])('resolves the stored name %s to %s', (stored, label) => {
    expect(sourceBadge(stored).label).toBe(label)
  })

  it('returns an unknown id as its own label', () => {
    expect(sourceBadge('okta').label).toBe('okta')
  })

  it('maps every catalog id with a collector to a stored name that has a badge', () => {
    expect(CATALOG_TO_SOURCE).toEqual({
      crowdstrike: 'crowdstrike',
      splunk: 'splunk',
      'elastic-siem': 'elastic',
      'azure-sentinel': 'azure_sentinel',
      'aws-security-hub': 'aws_security_hub',
      'microsoft-defender': 'microsoft_defender',
    })
    for (const stored of Object.values(CATALOG_TO_SOURCE)) {
      expect(sourceBadge(stored).label).not.toBe(stored)
    }
  })
})
