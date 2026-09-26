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
run_kind: investigate
objectives:
  - "Establish which accounts, tenants and resources the activity touched"
  - "Map the identity blast radius and any cross-account or cross-tenant pivot"
  - "Place the activity on the cloud kill chain and name the visibility gaps"
  - "Contain per provider and record it for regulatory exposure"
---

# Cloud Incident Investigation Workflow

Investigate a cloud security incident across AWS, Azure, and GCP. Establish which accounts, tenants, and resources the activity touched. Map the identity blast radius and any cross-account or cross-tenant pivot. Place the activity on the cloud kill chain and name the visibility gaps. Contain per provider and record it for regulatory exposure.
