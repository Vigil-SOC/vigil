import { useState } from 'react'
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  Box,
  Typography,
  Stepper,
  Step,
  StepLabel,
  Alert,
  FormControlLabel,
  Switch,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Chip,
  Accordion,
  AccordionSummary,
  AccordionDetails,
} from '@mui/material'
import {
  Check as CheckIcon,
  ExpandMore as ExpandMoreIcon,
} from '@mui/icons-material'

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

interface IntegrationWizardProps {
  open: boolean
  onClose: () => void
  integration: IntegrationMetadata
  onSave: (integrationId: string, config: Record<string, any>) => Promise<void>
  existingConfig?: Record<string, any>
}

export default function IntegrationWizard({
  open,
  onClose,
  integration,
  onSave,
  existingConfig = {},
}: IntegrationWizardProps) {
  const [activeStep, setActiveStep] = useState(0)
  const [config, setConfig] = useState<Record<string, any>>(existingConfig)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const steps = ['Configuration', 'Review']

  // Append the shared proxy block when this integration opts in.
  // Done here (rather than in integrations.ts) so the field list and
  // its rendering live in the same module.
  const effectiveFields: IntegrationField[] = integration.proxy_supported
    ? [...integration.fields, ...PROXY_FIELDS]
    : integration.fields

  const handleNext = () => {
    setActiveStep((prevActiveStep) => prevActiveStep + 1)
  }

  const handleBack = () => {
    setActiveStep((prevActiveStep) => prevActiveStep - 1)
  }

  const handleFieldChange = (fieldName: string, value: any) => {
    setConfig({ ...config, [fieldName]: value })
  }

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      await onSave(integration.id, config)
      onClose()
      // Reset state
      setActiveStep(0)
      setConfig({})
    } catch (err: any) {
      setError(err.message || 'Failed to save configuration')
    } finally {
      setSaving(false)
    }
  }

  const handleClose = () => {
    setActiveStep(0)
    setConfig(existingConfig)
    setError(null)
    onClose()
  }

  const isStepComplete = (step: number) => {
    if (step === 0) {
      // Check if all required fields are filled
      return effectiveFields
        .filter((f) => f.required)
        .every((f) => config[f.name] && config[f.name] !== '')
    }
    return true
  }

  const renderField = (field: IntegrationField) => {
    const value = config[field.name] ?? field.default ?? ''

    switch (field.type) {
      case 'boolean':
        return (
          <FormControlLabel
            key={field.name}
            control={
              <Switch
                checked={Boolean(value)}
                onChange={(e) => handleFieldChange(field.name, e.target.checked)}
              />
            }
            label={field.label}
          />
        )

      case 'select':
        return (
          <FormControl key={field.name} fullWidth margin="normal">
            <InputLabel>{field.label}</InputLabel>
            <Select
              value={value}
              label={field.label}
              onChange={(e) => handleFieldChange(field.name, e.target.value)}
            >
              {field.options?.map((opt) => (
                <MenuItem key={opt.value} value={opt.value}>
                  {opt.label}
                </MenuItem>
              ))}
            </Select>
            {field.helpText && (
              <Typography variant="caption" color="textSecondary" sx={{ mt: 0.5 }}>
                {field.helpText}
              </Typography>
            )}
          </FormControl>
        )

      case 'number':
        return (
          <TextField
            key={field.name}
            fullWidth
            type="number"
            label={field.label}
            value={value}
            onChange={(e) => handleFieldChange(field.name, parseInt(e.target.value))}
            margin="normal"
            required={field.required}
            placeholder={field.placeholder}
            helperText={field.helpText}
          />
        )

      default:
        return (
          <TextField
            key={field.name}
            fullWidth
            type={field.type === 'password' ? 'password' : 'text'}
            label={field.label}
            value={value}
            onChange={(e) => handleFieldChange(field.name, e.target.value)}
            margin="normal"
            required={field.required}
            placeholder={field.placeholder}
            helperText={field.helpText}
          />
        )
    }
  }

  const renderStepContent = (step: number) => {
    switch (step) {
      case 0:
        return (
          <Box>
            <Typography variant="body2" color="textSecondary" paragraph>
              {integration.description}
            </Typography>
            {integration.docs_url && (
              <Alert severity="info" sx={{ mb: 2 }}>
                Documentation:{' '}
                <a href={integration.docs_url} target="_blank" rel="noopener noreferrer">
                  {integration.docs_url}
                </a>
              </Alert>
            )}
            {effectiveFields
              .filter((field) => !field.section)
              .map((field) => renderField(field))}
            {Object.entries(
              effectiveFields
                .filter((field) => field.section)
                .reduce<Record<string, IntegrationField[]>>((groups, field) => {
                  const key = field.section as string
                  ;(groups[key] = groups[key] || []).push(field)
                  return groups
                }, {})
            ).map(([sectionName, fields]) => (
              <Accordion key={sectionName} defaultExpanded={false} sx={{ mt: 2 }}>
                <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                  <Typography>
                    {SECTION_LABELS[sectionName] || sectionName}
                  </Typography>
                </AccordionSummary>
                <AccordionDetails>
                  {fields.map((field) => renderField(field))}
                </AccordionDetails>
              </Accordion>
            ))}
          </Box>
        )

      case 1:
        return (
          <Box>
            <Typography variant="body2" color="textSecondary" paragraph>
              Review your configuration before saving:
            </Typography>
            <Box sx={{ mt: 2 }}>
              {effectiveFields.map((field) => {
                const value = config[field.name]
                const displayValue =
                  field.type === 'password' ? '••••••••' : value?.toString() || '(not set)'
                
                return (
                  <Box key={field.name} sx={{ mb: 2 }}>
                    <Typography variant="subtitle2">{field.label}</Typography>
                    <Typography variant="body2" color="textSecondary">
                      {displayValue}
                    </Typography>
                  </Box>
                )
              })}
            </Box>
          </Box>
        )

      default:
        return null
    }
  }

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="sm" fullWidth>
      <DialogTitle>
        Configure {integration.name}
        <Chip
          label={integration.category}
          size="small"
          color="error"
          sx={{ ml: 2 }}
        />
      </DialogTitle>

      <DialogContent>
        <Stepper activeStep={activeStep} sx={{ mb: 3 }}>
          {steps.map((label) => (
            <Step key={label}>
              <StepLabel>{label}</StepLabel>
            </Step>
          ))}
        </Stepper>

        {error && (
          <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
            {error}
          </Alert>
        )}

        {renderStepContent(activeStep)}
      </DialogContent>

      <DialogActions>
        <Button onClick={handleClose} disabled={saving}>
          Cancel
        </Button>
        {activeStep > 0 && (
          <Button onClick={handleBack} disabled={saving}>
            Back
          </Button>
        )}
        {activeStep < steps.length - 1 ? (
          <Button
            variant="contained"
            color="error"
            onClick={handleNext}
            disabled={!isStepComplete(activeStep)}
          >
            Next
          </Button>
        ) : (
          <Button
            variant="contained"
            color="error"
            onClick={handleSave}
            disabled={saving}
            startIcon={<CheckIcon />}
          >
            {saving ? 'Saving...' : 'Save Configuration'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  )
}

