import { findingsApi, type FindingDecisionResult, type FindingOutcome } from '../services/api'
import { newId } from './newId'

// A decision keeps its id until the server answers, so a second click or a retry
// after a lost reply replays the first instead of acting twice.
const unanswered = new Map<string, string>()

export async function decideFinding(findingId: string, outcome: FindingOutcome): Promise<FindingDecisionResult> {
  const key = `${findingId}:${outcome}`
  const request_id = unanswered.get(key) ?? newId()
  unanswered.set(key, request_id)
  const res = await findingsApi.decide(findingId, { outcome, request_id })
  unanswered.delete(key)
  return res.data
}
