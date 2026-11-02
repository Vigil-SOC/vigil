import { decideFinding } from './decideFinding'

export const SEND_TO_TRIAGE_FAILED = 'Couldn’t send this finding to triage'

// Queues the finding for the triage agent and returns the note to show the analyst.
export async function sendFindingToTriage(findingId: string): Promise<string> {
  const { already_queued } = await decideFinding(findingId, 'triage')
  return already_queued ? 'Already waiting in the Triage queue.' : 'Waiting in the Triage queue'
}
