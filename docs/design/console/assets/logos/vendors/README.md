# Vendor logos

Each file is the vendor's own mark, used only to name the vendor's product next
to its name. The marks remain their owners' trademarks; "public domain" below
is the copyright status Wikimedia Commons records, not a trademark licence.
Per PRD D6 the files ship unchanged into `clients/web/src/assets/vendors/`, and
the integration catalog points at them with a `logo` field. A vendor with no
file here shows a neutral category icon with its name.

The console draws each logo at 18 px on a white 28 px tile in both themes, so
files keep their brand colours and none is recoloured for dark mode. Where a
brand has a square symbol, the file is the symbol, not the wordmark.

| File | Vendor or product | Source | Licence or terms |
|---|---|---|---|
| `cloudtrail.svg` | AWS CloudTrail | AWS Architecture Icons (`Arch_AWS-CloudTrail_64`); in the package since #1298 | AWS Architecture Icons terms |
| `okta.svg` | Okta | Simple Icons, slug `okta`; in the package since #1298 | CC0-1.0 (icon data); Okta trademark |
| `splunk.svg` | Splunk | Simple Icons, slug `splunk`; in the package since #1298. Splunk publishes no square symbol in SVG, so this stays the wordmark | CC0-1.0 (icon data); Splunk trademark |
| `github.svg` | GitHub | Simple Icons 15.22.0, slug `github` (GitHub's one-colour mark, https://github.com/logos) | CC0-1.0 (icon data); GitHub trademark |
| `virustotal.svg` | VirusTotal | Simple Icons 15.22.0, slug `virustotal` (VirusTotal's one-colour mark) | CC0-1.0 (icon data); VirusTotal trademark |
| `paloalto.svg` | Palo Alto Networks | Simple Icons 15.22.0, slug `paloaltonetworks` (the one-colour mark) | CC0-1.0 (icon data); Palo Alto Networks trademark |
| `pagerduty.svg` | PagerDuty | Simple Icons 15.22.0, slug `pagerduty`, brand green `#06AC38` (https://www.pagerduty.com/brand/) | CC0-1.0 (icon data); PagerDuty trademark |
| `opensearch.svg` | OpenSearch | Simple Icons 15.22.0, slug `opensearch`, brand blue `#005EB8` (https://opensearch.org/trademark-brand-policy.html) | CC0-1.0 (icon data); OpenSearch trademark |
| `google-secops.svg` | Google Security Operations (Chronicle) | Simple Icons 15.22.0, slug `googlechronicle`, Google blue `#4285F4` | CC0-1.0 (icon data); Google trademark |
| `slack.svg` | Slack | https://commons.wikimedia.org/wiki/File:Slack_icon_2019.svg (full-colour symbol) | Public domain on Commons; Slack trademark |
| `google.svg` | Google (Google Threat Intelligence) | https://commons.wikimedia.org/wiki/File:Google_%22G%22_logo.svg (full-colour G) | Public domain on Commons; Google trademark |
| `jira.svg` | Jira | https://commons.wikimedia.org/wiki/File:Jira_Logo.svg, symbol only (the wordmark paths removed, the symbol's paths and gradients unchanged) | Public domain on Commons; Atlassian trademark |
| `sentinelone.svg` | SentinelOne | https://commons.wikimedia.org/wiki/File:SentinelOne_logo.svg, symbol only (the wordmark paths removed) | Public domain on Commons; SentinelOne trademark |
| `cloudflare.svg` | Cloudflare (also Cloudforce One) | https://commons.wikimedia.org/wiki/File:Cloudflare_Logo.svg, cloud symbol only (the wordmark paths removed) | Public domain on Commons; Cloudflare trademark (https://www.cloudflare.com/trademark/) |
| `microsoft-defender.svg` | Microsoft Defender | https://commons.wikimedia.org/wiki/File:Microsoft_Defender_Icon_Logo_2025_English.svg (Microsoft's 2025 icon) | Public domain on Commons; Microsoft trademark |
| `microsoft-teams.svg` | Microsoft Teams | https://commons.wikimedia.org/wiki/File:Microsoft_Office_Teams_(2025%E2%80%93present).svg | Public domain on Commons; Microsoft trademark |
| `microsoft-entra-id.svg` | Microsoft Entra ID (Azure Active Directory) | https://commons.wikimedia.org/wiki/File:Microsoft_Entra_ID_color_icon.svg | Public domain on Commons; Microsoft trademark |
| `microsoft-sentinel.svg` | Microsoft Sentinel | Azure Architecture Icons V24 (`10248-icon-service-Azure-Sentinel.svg`), https://learn.microsoft.com/azure/architecture/icons/ | Microsoft's icon terms: shown unmodified, with the product name beside it, only for that product. The terms name diagrams, training and documentation as the permitted uses |
| `aws-security-hub.svg` | AWS Security Hub | AWS Architecture Icons, 2026-07-31 package (`Arch_AWS-Security-Hub_64.svg`), https://aws.amazon.com/architecture/icons/ | AWS Architecture Icons terms (made for architecture diagrams) |
| `crowdstrike.svg` | CrowdStrike | The falcon from crowdstrike.com (`CS_Logo_Falcon.svg`, under `/content/dam/crowdstrike/marketing/en-us/images/graphics-and-illustrations/marketplace/`) | CrowdStrike trademark; CrowdStrike's own file |
| `cribl.svg` | Cribl | cribl.io site logo (`cribl-logo.svg` on images.ctfassets.net), symbol only (the wordmark path removed) | Cribl trademark; Cribl's own file |
| `elastic.svg` | Elastic | https://www.elastic.co/favicon.svg (the full-colour cluster symbol) | Elastic trademark; Elastic's own file |

The DeepTempo shield for LogLM is `../deeptempo-shield.svg`, as the Overview
board draws it.

No vendor-published SVG mark was found for Shodan, AlienVault OTX or MISP
(their own sites and Commons have only raster logos), or for Carbon Black,
Hybrid Analysis, Joe Sandbox, ANY.RUN, CAPE, Firecrawl, CloudCurrent VStrike
and Atomic Red Team. The VMware wordmark is unreadable at 18 px. These vendors
show the category icon until a mark is added.

To add a vendor: add its official mark here, add a row to this table, and set
`logo` on its catalog entry.
