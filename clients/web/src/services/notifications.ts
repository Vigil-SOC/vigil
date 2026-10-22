import { basePath } from '../config/basePath'

let enabled = false

function isSupported(): boolean {
  return 'Notification' in window
}

async function requestPermission(): Promise<boolean> {
  if (!isSupported()) {
    console.warn('Browser does not support notifications')
    return false
  }

  if (Notification.permission === 'granted') {
    return true
  }

  if (Notification.permission === 'denied') {
    console.warn('Notification permission was denied by user')
    return false
  }

  try {
    return (await Notification.requestPermission()) === 'granted'
  } catch (error) {
    console.error('Error requesting notification permission:', error)
    return false
  }
}

function setEnabled(next: boolean): void {
  enabled = next
}

function show(
  title: string,
  options: {
    body: string
    tag: string
    requireInteraction: boolean
    data: unknown
  },
): void {
  if (!enabled || !isSupported() || Notification.permission !== 'granted') {
    return
  }

  try {
    const notification = new Notification(title, {
      ...options,
      icon: `${basePath}/favicon.ico`,
    })

    if (!options.requireInteraction) {
      setTimeout(() => notification.close(), 10000)
    }
  } catch (error) {
    console.error('Error showing notification:', error)
  }
}

function notifyNewFinding(finding: {
  finding_id: string
  title?: string
  severity?: string
  description?: string
}): void {
  show('New Security Finding', {
    body: `${finding.severity?.toUpperCase() || 'UNKNOWN'}: ${finding.title || finding.description || finding.finding_id}`,
    tag: `finding-${finding.finding_id}`,
    requireInteraction: finding.severity === 'critical' || finding.severity === 'high',
    data: { type: 'finding', id: finding.finding_id },
  })
}

function notifyInvestigationComplete(investigation: {
  finding_id?: string
  case_id?: string
  title: string
  summary?: string
}): void {
  show('Investigation Complete', {
    body: `${investigation.title}${investigation.summary ? ': ' + investigation.summary : ''}`,
    tag: `investigation-${investigation.finding_id || investigation.case_id}`,
    requireInteraction: true,
    data: {
      type: 'investigation',
      finding_id: investigation.finding_id,
      case_id: investigation.case_id,
    },
  })
}

export const notificationService = {
  requestPermission,
  setEnabled,
  notifyNewFinding,
  notifyInvestigationComplete,
}
