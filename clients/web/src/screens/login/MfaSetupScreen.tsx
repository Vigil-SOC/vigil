import AuthLayout from './AuthLayout'
import TwoStepDialog from '../../shell/TwoStepDialog'

// Where the MFA requirement holds a person until they have set MFA up: the same
// setup the shell's two-step dialog offers, with nothing else reachable. It is
// never dismissed: the dialog ends by loading the console.
export default function MfaSetupScreen() {
  return (
    <AuthLayout
      screenLabel="MFA setup"
      title=""
      subtitle=""
      overlay={<TwoStepDialog open required onClose={() => {}} />}
    />
  )
}
