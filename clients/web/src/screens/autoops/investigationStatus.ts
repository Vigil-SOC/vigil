const LIVE = ['assigned', 'executing', 'waiting_approval', 'paused']
const FINISHED = ['completed', 'failed']

export const canKill = (status: string) => LIVE.includes(status)
export const canRestart = (status: string) => FINISHED.includes(status)
