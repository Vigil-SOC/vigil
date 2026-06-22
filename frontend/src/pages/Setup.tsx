// First-access setup page. Reached when SetupGate finds no working LLM
// provider. The redesign-styled SetupScreen owns the permission gate, the
// provider step, and the optional checklist; this page is just the route entry.
import SetupScreen from '../redesign/screens/setup/SetupScreen'

const Setup = () => <SetupScreen />

export default Setup
