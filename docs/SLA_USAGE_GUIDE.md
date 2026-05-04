# SLA Usage Guide - Quick Start

## Overview

This guide shows you how to add and manage SLA (Service Level Agreements) for cases in Vigil.

## Automatic SLA Assignment ✨

**Good News!** SLA is now **automatically assigned** when you create a case.

### How It Works

1. **Create a case** with any priority (critical, high, medium, low)
2. **SLA is automatically assigned** based on the default policy for that priority
3. **Countdown starts immediately** from case creation

**Example:**
```bash
# Create a high priority case
curl -X POST "http://localhost:6987/api/cases/" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Security Incident",
    "priority": "high",
    "finding_ids": ["FIND-001"],
    "description": "Critical security incident"
  }'

# SLA is automatically assigned using the default "high" priority policy
# Response deadline: 2 hours
# Resolution deadline: 8 hours
```

## Default SLA Policies

Your system comes with 4 pre-configured policies:

| Priority | Response Time | Resolution Time | Coverage |
|----------|---------------|-----------------|----------|
| Critical | 1 hour | 4 hours | 24/7 |
| High | 2 hours | 8 hours | 24/7 |
| Medium | 4 hours | 24 hours | Business hours |
| Low | 8 hours | 72 hours | Business hours |

## Manual SLA Assignment

If you need to override the automatic assignment:

### Via API

```bash
# Assign specific SLA policy to a case
curl -X POST "http://localhost:6987/api/cases/CASE-001/sla" \
  -H "Content-Type: application/json" \
  -d '{
    "sla_policy_id": "sla-vip-critical"
  }'

# Or use default for priority (same as automatic)
curl -X POST "http://localhost:6987/api/cases/CASE-001/sla" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Via Frontend (TypeScript/React)

```typescript
import { casesApi } from '@/services/api'

// Assign SLA to case
const assignSLA = async (caseId: string) => {
  try {
    // Use default policy for case priority
    await casesApi.assignSLA(caseId, {})
    
    // Or specify a policy
    await casesApi.assignSLA(caseId, {
      sla_policy_id: 'sla-vip-critical'
    })
    
    console.log('SLA assigned successfully')
  } catch (error) {
    console.error('Failed to assign SLA:', error)
  }
}
```

## Checking SLA Status

### Via API

```bash
# Get SLA status for a case
curl -X GET "http://localhost:6987/api/cases/CASE-001/sla"
```

**Response:**
```json
{
  "case_id": "CASE-001",
  "sla_policy_id": "sla-high-default",
  "response_due": "2024-01-20T16:30:00",
  "resolution_due": "2024-01-21T00:30:00",
  "response_remaining_seconds": 3600,
  "resolution_remaining_seconds": 25200,
  "response_percent_elapsed": 50.0,
  "resolution_percent_elapsed": 12.5,
  "response_completed": false,
  "resolution_completed": false,
  "response_sla_met": null,
  "resolution_sla_met": null,
  "is_breached": false,
  "breach_type": null,
  "is_paused": false,
  "health_status": "healthy"
}
```

### Via Frontend

```typescript
import { casesApi } from '@/services/api'

// Get SLA status
const checkSLA = async (caseId: string) => {
  try {
    const response = await casesApi.getSLA(caseId)
    const sla = response.data
    
    console.log(`Health: ${sla.health_status}`)
    console.log(`Response: ${sla.response_percent_elapsed}% elapsed`)
    console.log(`Resolution: ${sla.resolution_percent_elapsed}% elapsed`)
    
    if (sla.is_breached) {
      console.warn(`SLA BREACHED: ${sla.breach_type}`)
    }
  } catch (error) {
    console.error('Failed to get SLA:', error)
  }
}
```

## SLA Health Status

The system automatically calculates health status:

- **🟢 healthy**: < 75% of time elapsed
- **🟡 warning**: 75-89% of time elapsed
- **🟠 critical**: 90-99% of time elapsed
- **🔴 breached**: 100%+ of time elapsed (deadline passed)

## Pausing and Resuming SLA

Sometimes you need to pause the SLA clock (waiting for customer, external dependency, etc.):

### Via API

```bash
# Pause SLA
curl -X POST "http://localhost:6987/api/cases/CASE-001/sla/pause"

# Resume SLA
curl -X POST "http://localhost:6987/api/cases/CASE-001/sla/resume"
```

### Via Frontend

```typescript
import { casesApi } from '@/services/api'

// Pause SLA
await casesApi.pauseSLA(caseId)

// Resume SLA
await casesApi.resumeSLA(caseId)
```

**Important Notes:**
- When paused, the SLA timer stops
- Deadlines are automatically extended by the pause duration when resumed
- Total pause time is tracked for reporting

## Managing SLA Policies

### List All Policies

```bash
# Get all active policies
curl -X GET "http://localhost:6987/api/sla-policies/?active_only=true"

# Get default policies only
curl -X GET "http://localhost:6987/api/sla-policies/?default_only=true"

# Get policies for specific priority
curl -X GET "http://localhost:6987/api/sla-policies/?priority_level=high"
```

### Create Custom Policy

```bash
curl -X POST "http://localhost:6987/api/sla-policies/" \
  -H "Content-Type: application/json" \
  -d '{
    "policy_id": "sla-vip-high",
    "name": "VIP High Priority SLA",
    "description": "Enhanced SLA for VIP customers",
    "priority_level": "high",
    "response_time_hours": 1.0,
    "resolution_time_hours": 4.0,
    "business_hours_only": false,
    "notification_thresholds": [50, 75, 90, 100],
    "is_active": true,
    "is_default": false
  }'
```

### Update Policy

```bash
curl -X PUT "http://localhost:6987/api/sla-policies/sla-high-default" \
  -H "Content-Type: application/json" \
  -d '{
    "response_time_hours": 1.5,
    "resolution_time_hours": 6.0
  }'
```

### Set as Default

```bash
# Make this the default policy for its priority level
curl -X POST "http://localhost:6987/api/sla-policies/sla-vip-high/set-default"
```

## Frontend Integration

### Using the CaseSLA Component

The system includes a pre-built React component for displaying SLA status:

```tsx
import CaseSLA from '@/components/cases/CaseSLA'

function CaseDetailView({ caseId }: { caseId: string }) {
  return (
    <div>
      <h1>Case Details</h1>
      
      {/* SLA Component - shows countdown, progress, pause/resume */}
      <CaseSLA caseId={caseId} />
      
      {/* Rest of case details */}
    </div>
  )
}
```

**Features:**
- Real-time countdown timer
- Visual progress bar
- Color-coded status (green → yellow → orange → red)
- Pause/resume buttons
- Breach alerts

### Using the SLA Policies API

```typescript
import { slaPoliciesApi } from '@/services/api'

// List all policies
const policies = await slaPoliciesApi.getAll({ active_only: true })

// Get specific policy
const policy = await slaPoliciesApi.getById('sla-high-default')

// Create new policy
await slaPoliciesApi.create({
  policy_id: 'sla-custom-001',
  name: 'Custom Policy',
  priority_level: 'high',
  response_time_hours: 1.5,
  resolution_time_hours: 6.0,
  business_hours_only: false
})

// Update policy
await slaPoliciesApi.update('sla-custom-001', {
  response_time_hours: 2.0
})

// Get usage statistics
const usage = await slaPoliciesApi.getUsage('sla-high-default')
console.log(`Compliance rate: ${usage.compliance_rate}%`)

// Get cases using this policy
const cases = await slaPoliciesApi.getCases('sla-high-default', {
  breached_only: true
})
```

## Common Workflows

### Workflow 1: Create Case with Custom SLA

```typescript
// 1. Create the case
const caseResponse = await casesApi.create({
  title: 'VIP Customer Issue',
  priority: 'high',
  finding_ids: ['FIND-001'],
  description: 'VIP customer reported issue'
})

const caseId = caseResponse.data.case_id

// 2. Override with VIP SLA policy
await casesApi.assignSLA(caseId, {
  sla_policy_id: 'sla-vip-high'
})
```

### Workflow 2: Monitor SLA Health

```typescript
// Get all cases with SLA issues
const breachedCases = await casesApi.getAll({
  status: 'open'
})

for (const case of breachedCases.data.cases) {
  const sla = await casesApi.getSLA(case.case_id)
  
  if (sla.data.health_status === 'critical') {
    console.warn(`Case ${case.case_id} is at risk!`)
    // Send alert, escalate, etc.
  }
}
```

### Workflow 3: Pause During External Wait

```typescript
// Customer needs to provide information
await casesApi.addComment(caseId, {
  content: 'Waiting for customer response',
  author: 'analyst@company.com'
})

// Pause SLA while waiting
await casesApi.pauseSLA(caseId)

// Later, when customer responds...
await casesApi.addComment(caseId, {
  content: 'Customer provided requested information',
  author: 'analyst@company.com'
})

// Resume SLA
await casesApi.resumeSLA(caseId)
```

## Testing SLA Assignment

A test script is provided to verify SLA functionality:

```bash
# Run the test suite
python scripts/test_sla_assignment.py
```

**Tests included:**
1. ✓ List SLA policies
2. ✓ Automatic SLA assignment
3. ✓ Manual SLA assignment
4. ✓ Pause/resume functionality

## Troubleshooting

### SLA Not Assigned Automatically

**Check:**
1. Is there a default policy for the case priority?
   ```bash
   curl -X GET "http://localhost:6987/api/sla-policies/?priority_level=high&default_only=true"
   ```

2. Check backend logs for errors:
   ```bash
   tail -f logs/backend.log | grep SLA
   ```

### SLA Assignment Failed

**Common causes:**
- No default policy exists for the priority level
- Policy is inactive (`is_active: false`)
- Database connection issue

**Solution:**
```bash
# Create or activate a default policy
curl -X PUT "http://localhost:6987/api/sla-policies/sla-high-default" \
  -H "Content-Type: application/json" \
  -d '{"is_active": true, "is_default": true}'
```

### SLA Shows as Breached Immediately

**Possible causes:**
- System clock is incorrect
- Case was created in the past
- SLA policy times are too short

**Check:**
```bash
# Verify policy times
curl -X GET "http://localhost:6987/api/sla-policies/sla-high-default"

# Check case creation time
curl -X GET "http://localhost:6987/api/cases/CASE-001"
```

## Best Practices

### 1. Use Automatic Assignment
Let the system automatically assign SLA based on priority. Only override for special cases.

### 2. Document Pauses
Always add a comment when pausing SLA to explain why:
```typescript
await casesApi.addComment(caseId, {
  content: 'Pausing SLA - waiting for vendor response',
  author: 'analyst@company.com'
})
await casesApi.pauseSLA(caseId)
```

### 3. Monitor Health Status
Set up alerts for cases with `health_status: 'critical'` to prevent breaches.

### 4. Review Policies Regularly
Check compliance rates and adjust policies:
```bash
# Get compliance report
curl -X GET "http://localhost:6987/api/cases/metrics/sla-compliance"
```

### 5. Test Before Production
Always test new policies with a few cases before setting as default.

## API Reference

For complete API documentation, see:
- [SLA Policy API Guide](./SLA_POLICY_API_GUIDE.md)
- [SLA System Enhancement Plan](./SLA_SYSTEM_ENHANCEMENT_PLAN.md)
- [SLA System Assessment](./SLA_SYSTEM_ASSESSMENT.md)

## Support

For issues or questions:
1. Check the logs: `logs/backend.log`
2. Run the test script: `python scripts/test_sla_assignment.py`
3. Review the API documentation
4. Check database for policy configuration

---

**Last Updated**: January 21, 2026  
**Version**: 1.0.0

