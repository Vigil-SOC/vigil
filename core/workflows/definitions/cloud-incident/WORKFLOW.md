---
name: cloud-incident
description: "Investigate and respond to cloud security incidents across AWS, Azure, and GCP. Covers identity blast-radius, IAM/role analysis, control-plane vs data-plane attacks, cross-account/cross-tenant pivots, and provider-aware containment."
agents:
  - investigator
  - correlator
  - mitre_analyst
  - responder
  - reporter
tools-used:
  - get_finding
  - list_findings
  - nearest_neighbors
  - search_detections
  - get_technique_rollup
  - create_attack_layer
  - create_case
  - create_approval_action
  - update_case
  - get_case
use-case: "Cloud-native incident response — compromised credentials, IAM policy abuse, unauthorized data access, cross-account pivoting, or control-plane attacks in AWS, Azure, or GCP."
trigger-examples:
  - "Run cloud incident response on finding f-20260215-abc123"
  - "Investigate this suspicious IAM activity in AWS"
  - "Cloud incident: unauthorized S3 access from external IP"
  - "Respond to Azure AD credential compromise alert"
  - "Run cloud-incident workflow for this GCP SCC finding"
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

## Agent Sequence

### Phase 1: Cloud Evidence Gathering (Investigator Agent)

**Purpose:** Root-cause analysis in cloud environments — collect audit logs, enumerate affected resources, and determine control-plane vs data-plane scope.

**Tools:** `get_finding`, `list_findings`, `nearest_neighbors`, `search_detections`

**Steps:**
1. Fetch the target finding via `get_finding` and identify the cloud provider (AWS, Azure, GCP)
2. Identify affected scope: account ID / subscription ID / project ID, region(s), organization/tenant
3. Gather cloud audit logs:
   - AWS: CloudTrail (management events + data events if available)
   - Azure: Activity Logs + Azure Diagnostics
   - GCP: Cloud Audit Logs (Admin Activity + Data Access)
4. Determine attack plane:
   - Control-plane: IAM changes, role assumption, policy modifications, resource creation/deletion
   - Data-plane: direct object access (S3, Blob, GCS), database queries, compute metadata API abuse
5. Enumerate affected resources: EC2 instances, S3 buckets, Lambda functions, Azure VMs, Storage Accounts, Entra users, GCP Compute Engine, Cloud Storage, service accounts
6. Collect IAM/identity evidence: role assumption chains, access key usage, OAuth token grants, SAML/SSO sign-ins, conditional access failures
7. Identify initial access vector: compromised credentials, leaked keys, instance metadata service abuse, supply chain, misconfigured bucket
8. Use `nearest_neighbors` to find related findings via embedding similarity
9. Document chain of evidence with cloud-native identifiers (ARNs, resource IDs, subscription IDs)

**Output:** Cloud provider(s), affected accounts/subscriptions/projects, resource inventory, control-plane vs data-plane scope, IAM evidence chain, initial access vector, related findings

### Phase 2: Cross-Cloud Correlation (Correlator Agent)

**Purpose:** Link cloud events across providers, accounts, and tenants. Identify identity blast-radius and cross-account/cross-tenant pivot attempts.

**Tools:** `list_findings`, `create_case`, `get_technique_rollup`, `nearest_neighbors`

**Steps:**
1. Gather all findings from Phase 1 and search for correlated alerts via `list_findings`
2. Correlate by identity blast-radius:
   - Compromised IAM user/role → assumed roles in other accounts (AWS STS)
   - Compromised Azure AD user → guest access in other tenants, B2B collaborations
   - Compromised GCP service account → cross-project IAM bindings
3. Detect cross-account / cross-tenant pivot attempts:
   - Look for STS `AssumeRole` / `AssumeRoleWithSAML` / `AssumeRoleWithWebIdentity` to external accounts
   - Look for Azure AD invitations to external domains
   - Look for GCP IAM policy changes granting external principals
4. Correlate control-plane API calls with data-plane exfiltration:
   - Match `PutBucketPolicy` / `SetContainerACL` events with subsequent large data transfers
   - Match `CreateAccessKey` events with immediate API usage from new IPs
5. Assess blast radius by IAM trust boundaries and resource hierarchy:
   - AWS: OU/SCP boundaries, IAM trust policies, service-linked roles
   - Azure: Management group hierarchy, RBAC assignments, PIM elevations
   - GCP: Organization policy constraints, IAM hierarchy, custom roles
6. Score correlation strength:
   - Time proximity (within minutes/hours): +0.2
   - Entity overlap (shared IAM roles, service accounts, source IPs): +0.3
   - Cross-account/tenant technique chain: +0.4
7. Group correlated alerts into a case via `create_case`

**Output:** Correlated alert groups, identity blast-radius map, cross-account/tenant pivot indicators, attack chain narrative, correlation scores, new case groupings

### Phase 3: Cloud ATT&CK Mapping (MITRE Analyst Agent)

**Purpose:** Map cloud TTPs to MITRE ATT&CK, with emphasis on cloud-specific techniques and kill-chain progression in multi-tenant environments.

**Tools:** `get_finding`, `get_technique_rollup`, `create_attack_layer`

**Steps:**
1. Extract all MITRE technique IDs from findings and related alerts
2. Map techniques to cloud-specific ATT&CK tactics and techniques:
   - **Initial Access:** T1078.004 Valid Accounts: Cloud Accounts, T1078.005 Valid Accounts: Cloud Accounts (Intermittent), T1566 Phishing (cloud console credentials)
   - **Execution:** T1059.008 Command and Scripting Interpreter: Network Device CLI (AWS CLI, Azure CLI, gcloud), T1648 Serverless Execution (Lambda, Azure Functions, Cloud Functions)
   - **Persistence:** T1098.001 Additional Cloud Credentials, T1098.002 Additional Email Delegate Permissions, T1136.003 Create Account: Cloud Account
   - **Privilege Escalation:** T1078.004 Valid Accounts: Cloud Accounts (role assumption), T1484 Domain Policy Modification (Azure AD), T1548 Abuse Elevation Control Mechanism (GCP IAM conditions)
   - **Defense Evasion:** T1535 Unused/Unsupported Cloud Regions, T1562.008 Impair Defenses: Disable Cloud Logs, T1659 Content Delivery Network (CDN) abuse
   - **Credential Access:** T1528 Steal Application Access Token, T1552.001 Credentials In Files (instance user-data, Azure Key Vault), T1652 Cloud Instance Metadata API
   - **Discovery:** T1526 Cloud Service Discovery, T1613 Container and Resource Discovery, T1069.003 Permission Groups Discovery: Cloud Groups
   - **Lateral Movement:** T1078.004 Valid Accounts: Cloud Accounts (cross-account), T1550 Use Alternate Authentication Material (OAuth tokens, SAML assertions)
   - **Collection:** T1530 Data from Cloud Storage Object, T1213.003 Data from Information Repositories: Code Repositories
   - **Exfiltration:** T1567.002 Exfiltration to Cloud Storage, T1048.003 Exfiltration Over Alternative Protocol: Exfiltration Over Unencrypted/Obfuscated Non-C2 Protocol
3. Assess kill chain progression in cloud context:
   - How far has the attacker advanced within the cloud environment?
   - Have they achieved persistent access to the control plane?
   - Have they escalated to organization-level privileges?
4. Identify gaps in cloud detection coverage:
   - Missing CloudTrail data events, missing Azure Diagnostics, missing GCP Data Access logs
   - Gaps in IAM change alerting, missing cross-account anomaly detection
5. Evaluate adversary sophistication based on cloud TTPs:
   - Script-kiddie: exposed keys, public S3 buckets
   - APT: instance metadata service abuse, cross-account role chaining, log tampering
6. Generate ATT&CK Navigator layer visualization highlighting cloud techniques
7. Recommend detection rules for coverage gaps (Sigma, CloudWatch Alarms, Azure Monitor, GCP Security Health Analytics)

**Output:** Technique IDs with confidence, kill chain stage assessment, cloud ATT&CK Navigator layer, detection coverage gaps, adversary sophistication profile

### Phase 4: Cloud Containment & Response (Responder Agent)

**Purpose:** Execute provider-aware containment actions with confidence scoring and approval gating.

**Tools:** `create_approval_action`, `update_case`, `get_finding`

**Steps:**
1. Review investigation results and affected entities from Phases 1-3
2. Assess blast radius — what accounts, tenants, resources, and identities are at risk
3. Plan provider-aware containment actions with confidence scores:
   - **0.95-1.0:** Critical threat (active data exfiltration, confirmed control-plane compromise) — auto-approve
   - **0.85-0.94:** High confidence (confirmed unauthorized cross-account role assumption) — quick review
   - **0.70-0.84:** Moderate (suspicious API pattern from new IP) — human approval required
   - Below 0.70: Needs more investigation
4. Identity containment:
   - Revoke sessions (Azure AD / Entra, GCP OAuth tokens)
   - Disable compromised IAM users/roles (AWS IAM, Azure AD, GCP IAM)
   - Rotate exposed access keys and disable leaked credentials
   - Revoke SAML/OAuth tokens if identity provider is compromised
5. Network containment:
   - Isolate instances: AWS Security Group quarantine, Azure NSG deny-all, GCP firewall deny
   - Block malicious IPs at Cloudflare WAF (`cf_waf_block_ip`) or Gateway (`cf_gateway_block_domain`)
6. Data containment:
   - Restrict S3/Blob/GCS bucket policies (remove public access, tighten ACLs)
   - Enable bucket versioning and object lock if ransomware is suspected
7. Forensic preservation:
   - Snapshot compromised EC2/Azure VM/GCE instances before isolation
   - Preserve CloudTrail/Activity Log/Audit Log exports for legal hold
8. Submit containment actions via `create_approval_action`
9. Define eradication steps: remove rogue IAM policies, close metadata service vulnerabilities, patch misconfigurations
10. Plan recovery and monitoring: re-enable services with hardened configs, enable additional logging

**Output:** Provider-aware containment actions with confidence scores, approval requests, remediation checklist, forensic preservation plan, blast radius assessment

### Phase 5: Cloud Incident Report (Reporter Agent)

**Purpose:** Generate a cloud-focused incident report with executive, technical, and compliance sections tailored to multi-cloud incidents.

**Tools:** `get_case`, `list_findings`, `create_attack_layer`

**Steps:**
1. Gather all data from prior phases (case, findings, actions taken, cloud audit logs)
2. Generate final MITRE ATT&CK Navigator layer for the incident
3. Structure the cloud incident report:
   - **Executive Summary:** Business impact in plain language, including cloud spend impact, regulatory exposure (GDPR, HIPAA, PCI-DSS, SOC 2), and customer/data-subject impact
   - **Cloud Environment Overview:** Affected providers, accounts/subscriptions/projects, regions, and resource inventory
   - **Technical Details:** Evidence chain for the security team, including ARN/resource ID references, IAM change timeline, and control-plane vs data-plane analysis
   - **Timeline:** Chronological reconstruction of cloud audit log events
   - **Identity Blast-Radius:** Compromised identities, role assumption chains, cross-account/tenant pivots
   - **MITRE ATT&CK Analysis:** Cloud-specific techniques, tactics, kill chain progression, and detection gaps
   - **Correlation Results:** Attack chains, cross-account/tenant movements, campaign identification
   - **Affected Assets:** Complete entity inventory organized by provider, account, and region
   - **Containment Actions:** Provider-specific response measures taken (AWS, Azure, GCP, Cloudflare)
   - **Recommendations:**
     - IAM hardening: least-privilege roles, MFA enforcement, temporary credentials, service account key rotation
     - Logging and detection: enable CloudTrail data events, Azure Diagnostics, GCP Data Access logs, cross-account anomaly alerts
     - Architecture: VPC isolation, private endpoints, metadata service v2 (IMDSv2), workload identity federation
   - **Lessons Learned:** What to improve in cloud security posture and incident response playbooks

**Output:** Complete cloud incident report, cloud ATT&CK Navigator layer, event timeline, identity blast-radius map, recommendations

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
