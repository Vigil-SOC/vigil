---
name: cloud-incident
description: "Investigate and respond to cloud security incidents across AWS, Azure, and GCP. Covers identity blast-radius, IAM/role analysis, control-plane vs data-plane attacks, cross-account/cross-tenant pivots, and provider-aware containment."
use_case: "Cloud-native incident response \u2014 compromised credentials, IAM policy abuse, unauthorized data access, cross-account pivoting, or control-plane attacks in AWS, Azure, or GCP."
trigger_examples:
  - "Run cloud incident response on finding f-20260215-abc123"
  - "Investigate this suspicious IAM activity in AWS"
  - "Cloud incident: unauthorized S3 access from external IP"
  - "Respond to Azure AD credential compromise alert"
  - "Run cloud-incident workflow for this GCP SCC finding"
objectives:
  - "Establish which accounts, tenants and resources the activity touched"
  - "Map the identity blast radius and any cross-account or cross-tenant pivot"
  - "Place the activity on the cloud kill chain and name the visibility gaps"
  - "Contain per provider and record it for regulatory exposure"
phases:
  - id: evidence-gathering
    agent: investigator
    name: "Cloud Evidence Gathering"
    tools: [get_finding, list_findings, search_detections, recall_entity]
    instructions: |
      Root-cause analysis in cloud environments: collect audit logs, enumerate
      affected resources, and determine control-plane vs data-plane scope.

      1. Fetch the target finding via `get_finding` and identify the provider
         (AWS, Azure, GCP)
      2. Identify affected scope: account / subscription / project ID, regions,
         organisation or tenant
      3. Gather cloud audit logs:
         - AWS: CloudTrail (management events, plus data events if available)
         - Azure: Activity Logs and Azure Diagnostics
         - GCP: Cloud Audit Logs (Admin Activity and Data Access)
      4. Determine the attack plane:
         - Control-plane: IAM changes, role assumption, policy modifications,
           resource creation or deletion
         - Data-plane: direct object access (S3, Blob, GCS), database queries,
           compute metadata API abuse
      5. Enumerate affected resources across the providers in scope
      6. Collect IAM/identity evidence: role assumption chains, access key usage,
         OAuth token grants, SAML/SSO sign-ins, conditional access failures
      7. Identify the initial access vector: compromised credentials, leaked
         keys, instance metadata service abuse, supply chain, misconfigured bucket
      8. Document the evidence chain with cloud-native identifiers (ARNs,
         resource IDs, subscription IDs)

      Where a log source was not enabled, that is a visibility gap and belongs in
      the handoff: absent Data Access logs are not an absence of data access.

  - id: correlation
    agent: correlator
    name: "Cross-Cloud Correlation"
    tools: [list_findings, create_case, get_technique_rollup, recall_entity]
    instructions: |
      Link cloud events across providers, accounts and tenants. Identify identity
      blast-radius and cross-account or cross-tenant pivot attempts.

      1. Gather the findings from the previous step and search for correlated
         alerts via `list_findings`
      2. Correlate by identity blast-radius:
         - Compromised IAM user/role -> assumed roles in other accounts (AWS STS)
         - Compromised Entra user -> guest access in other tenants, B2B collaborations
         - Compromised GCP service account -> cross-project IAM bindings
      3. Detect cross-account and cross-tenant pivot attempts:
         - STS `AssumeRole` / `AssumeRoleWithSAML` / `AssumeRoleWithWebIdentity`
           to external accounts
         - Entra invitations to external domains
         - GCP IAM policy changes granting external principals
      4. Correlate control-plane API calls with data-plane exfiltration:
         - Match `PutBucketPolicy` / `SetContainerACL` with subsequent large transfers
         - Match `CreateAccessKey` with immediate API usage from new IPs
      5. Assess blast radius by IAM trust boundaries and resource hierarchy
      6. Score correlation strength:
         - Time proximity (within minutes/hours): +0.2
         - Entity overlap (shared roles, service accounts, source IPs): +0.3
         - Cross-account/tenant technique chain: +0.4
      7. Group correlated alerts into a case via `create_case`

  - id: attack-mapping
    agent: mitre_analyst
    name: "Cloud ATT&CK Mapping"
    tools: [get_finding, get_technique_rollup, create_attack_layer, recall_entity]
    instructions: |
      Map cloud TTPs to MITRE ATT&CK, emphasising cloud-specific techniques and
      kill-chain progression in multi-tenant environments.

      1. Extract all MITRE technique IDs from findings and related alerts
      2. Map to cloud-specific tactics and techniques, among them:
         - **Initial Access:** T1078.004 Valid Accounts: Cloud Accounts, T1566 Phishing
         - **Execution:** T1059.008 (AWS/Azure/gcloud CLI), T1648 Serverless Execution
         - **Persistence:** T1098.001 Additional Cloud Credentials, T1136.003 Create Cloud Account
         - **Privilege Escalation:** T1078.004 role assumption, T1484 Domain Policy Modification
         - **Defense Evasion:** T1535 Unused Cloud Regions, T1562.008 Disable Cloud Logs
         - **Credential Access:** T1528 Steal Application Access Token, T1652 Cloud Instance Metadata API
         - **Discovery:** T1526 Cloud Service Discovery, T1613 Container and Resource Discovery
         - **Lateral Movement:** T1078.004 cross-account, T1550 Use Alternate Authentication Material
         - **Collection:** T1530 Data from Cloud Storage Object
         - **Exfiltration:** T1567.002 Exfiltration to Cloud Storage, T1048.003
      3. Assess kill chain progression: how far into the environment, is control-plane
         persistence established, have organisation-level privileges been reached?
      4. Identify gaps in cloud detection coverage: missing CloudTrail data events,
         missing Azure Diagnostics, missing GCP Data Access logs, absent IAM alerting
      5. Evaluate adversary sophistication from the TTPs, from exposed keys and
         public buckets through to metadata service abuse, role chaining and log tampering
      6. Generate an ATT&CK Navigator layer highlighting the cloud techniques
      7. Recommend detection rules for the coverage gaps

  - id: containment
    agent: responder
    name: "Cloud Containment & Response"
    tools: [create_approval_action, update_case, get_finding, recall_entity]
    approval_required: true
    instructions: |
      Execute provider-aware containment with confidence scoring and approval
      gating.

      1. Review the results and affected entities from the earlier steps
      2. Assess blast radius: which accounts, tenants, resources and identities
         are at risk
      3. Plan provider-aware containment with confidence scores:
         - **0.95-1.0:** critical (active exfiltration, confirmed control-plane
           compromise) -- auto-approve
         - **0.85-0.94:** high confidence (confirmed unauthorised cross-account
           role assumption) -- quick review
         - **0.70-0.84:** moderate (suspicious API pattern from a new IP) --
           human approval required
         - Below 0.70: needs more investigation
      4. Identity containment: revoke sessions, disable compromised users and
         roles, rotate exposed keys, revoke SAML/OAuth tokens
      5. Network containment: isolate instances (Security Group quarantine, NSG
         deny-all, GCP firewall deny), block malicious IPs at the WAF or Gateway
      6. Data containment: restrict bucket policies, remove public access, enable
         versioning and object lock where ransomware is suspected
      7. Forensic preservation: snapshot compromised instances before isolation,
         preserve audit log exports for legal hold
      8. Submit containment actions via `create_approval_action`
      9. Define eradication: remove rogue IAM policies, close metadata service
         exposure, patch misconfigurations
      10. Plan recovery and monitoring: re-enable with hardened configuration and
          additional logging

      Preserve before you isolate. An instance terminated for containment takes
      its evidence with it.

  - id: report
    agent: reporter
    name: "Cloud Incident Report"
    tools: [get_case, list_findings, create_attack_layer, recall_entity]
    instructions: |
      Produce a cloud-focused incident report with executive, technical and
      compliance sections.

      1. Gather everything the earlier steps produced
      2. Generate the final MITRE ATT&CK Navigator layer
      3. Structure the report:
         - **Executive Summary:** business impact in plain language, including
           cloud spend impact, regulatory exposure (GDPR, HIPAA, PCI-DSS, SOC 2)
           and customer or data-subject impact
         - **Cloud Environment Overview:** providers, accounts, regions, inventory
         - **Technical Details:** evidence chain with ARN and resource IDs, the
           IAM change timeline, control-plane vs data-plane analysis
         - **Timeline:** chronological reconstruction of audit log events
         - **Identity Blast-Radius:** compromised identities, role assumption
           chains, cross-account and cross-tenant pivots
         - **MITRE ATT&CK Analysis:** techniques, tactics, progression, gaps
         - **Correlation Results:** attack chains, cross-account movement, campaigns
         - **Affected Assets:** inventory by provider, account and region
         - **Containment Actions:** provider-specific measures taken
         - **Recommendations:** IAM hardening, logging and detection, architecture
         - **Lessons Learned:** posture and playbook improvements

      Regulatory exposure rests on what was actually established. Separate
      confirmed access from possible access: a notification decision made on a
      hedge is worse than one made on a gap you named.
---

# Cloud Incident Investigation Workflow

Multi-agent cloud incident response workflow. Sequences five specialized agents to gather cloud-specific evidence, correlate across accounts and tenants, map to cloud MITRE ATT&CK techniques, execute provider-aware containment, and produce a cloud-focused incident report.

## When to Use

- A cloud security alert has fired (GuardDuty, Security Hub, Azure Sentinel, GCP SCC, Chronicle)
- IAM or identity-related anomaly is detected in AWS, Azure, or GCP
- Unauthorized data access or exfiltration from cloud storage (S3, Blob, GCS)
- Cross-account or cross-tenant suspicious activity is observed
- Control-plane API abuse is detected (CloudTrail, Azure Activity Logs, GCP Audit Logs)
- A finding involves cloud credentials, service accounts, or federation tokens

## Example Invocation

```
User: "Run cloud incident response on finding f-20260215-a1b2c3d4"
```

## Expected Output

```json
{
  "workflow": "cloud-incident",
  "phases_completed": ["evidence-gathering", "correlation", "attack-mapping", "containment", "report"],
  "cloud_providers": ["aws", "azure"],
  "affected_scope": {
    "aws": {
      "account_id": "123456789012",
      "regions": ["us-east-1", "eu-west-1"],
      "resources": ["arn:aws:iam::123456789012:role/AdminRole", "arn:aws:s3:::data-bucket"]
    },
    "azure": {
      "subscription_id": "sub-abc123",
      "tenant_id": "tenant-xyz789",
      "resources": ["Azure AD user: admin@corp.com", "Storage Account: corpdata"]
    }
  },
  "attack_plane": "control-plane",
  "identity_blast_radius": {
    "compromised_identities": ["arn:aws:iam::123456789012:user/breach-user", "admin@corp.com"],
    "cross_account_pivots": 2,
    "cross_tenant_pivots": 0
  },
  "mitre_techniques": ["T1078.004", "T1526", "T1098.001", "T1567.002", "T1530"],
  "kill_chain_stage": "lateral_movement",
  "containment_actions": [
    {"action": "disable_iam_user", "target": "breach-user", "provider": "aws", "confidence": 0.97, "status": "auto-approved"},
    {"action": "revoke_sessions", "target": "admin@corp.com", "provider": "azure", "confidence": 0.95, "status": "auto-approved"},
    {"action": "block_ip", "target": "185.220.101.1", "provider": "cloudflare", "confidence": 0.92, "status": "quick-review"}
  ],
  "report_sections": ["executive_summary", "environment_overview", "technical_details", "timeline", "identity_blast_radius", "mitre_analysis", "correlation", "affected_assets", "containment_actions", "recommendations", "lessons_learned"]
}
```
