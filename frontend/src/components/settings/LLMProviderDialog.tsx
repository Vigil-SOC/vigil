import { Dialog, DialogTitle, DialogContent } from '@mui/material'
import { LLMProvider } from '../../services/api'
import ProviderConfigSteps from './ProviderConfigSteps'

interface Props {
  existing: LLMProvider | null
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}

// MUI modal shell around the ProviderConfigSteps core, used by the Settings LLM
// tab (LLMProvidersTab).
// NOTE: the redesign onboarding wizard (/setup) does NOT reuse this core — it
// has its own Tailwind implementation (redesign/screens/settings/
// LlmProviderDialog) that has diverged; reconcile after #352.
const LLMProviderDialog = ({ existing, onClose, onSaved, onError }: Props) => {
  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{existing ? 'Edit provider' : 'Add LLM provider'}</DialogTitle>
      <DialogContent>
        <ProviderConfigSteps
          existing={existing}
          onCancel={onClose}
          onSaved={onSaved}
          onError={onError}
        />
      </DialogContent>
    </Dialog>
  )
}

export default LLMProviderDialog
