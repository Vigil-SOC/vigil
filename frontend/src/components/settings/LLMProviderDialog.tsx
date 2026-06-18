import { Dialog, DialogTitle, DialogContent } from '@mui/material'
import { LLMProvider } from '../../services/api'
import ProviderConfigSteps from './ProviderConfigSteps'

interface Props {
  existing: LLMProvider | null
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}

// Modal shell around the shared ProviderConfigSteps core. The full-page
// onboarding wizard (pages/Setup) renders the same core without this frame.
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
