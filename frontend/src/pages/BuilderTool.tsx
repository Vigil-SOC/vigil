import { useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Box, Tab, Tabs, Snackbar, Alert, Dialog, DialogTitle, DialogContent, DialogContentText, DialogActions, Button } from '@mui/material'

import WorkflowBuilder from './WorkflowBuilder'
import AgentBuilderTab from '../components/settings/AgentBuilderTab'
import SkillsTab from '../components/settings/SkillsTab'

type SubTab = 'workflows' | 'agents' | 'skills'

const TAB_ORDER: SubTab[] = ['workflows', 'agents', 'skills']
const TAB_LABELS: Record<SubTab, string> = {
  workflows: 'Workflows',
  agents: 'Agents',
  skills: 'Skills',
}

function isSubTab(v: string | null): v is SubTab {
  return v === 'workflows' || v === 'agents' || v === 'skills'
}

export default function BuilderTool() {
  const navigate = useNavigate()
  const location = useLocation()

  const initialTab = useMemo<SubTab>(() => {
    const q = new URLSearchParams(location.search).get('tab')
    return isSubTab(q) ? q : 'workflows'
  }, [location.search])

  const [tab, setTab] = useState<SubTab>(initialTab)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [confirmDialog, setConfirmDialog] = useState<{ open: boolean; title: string; msg: string; onConfirm: () => void }>({
    open: false,
    title: '',
    msg: '',
    onConfirm: () => {},
  })

  const handleTabChange = (_: unknown, next: SubTab) => {
    setTab(next)
    const params = new URLSearchParams(location.search)
    params.set('tab', next)
    navigate({ pathname: location.pathname, search: `?${params.toString()}` }, { replace: true })
  }

  const showConfirm = (title: string, msg: string, onConfirm: () => void) => {
    setConfirmDialog({ open: true, title, msg, onConfirm })
  }
  const closeConfirm = () => setConfirmDialog(prev => ({ ...prev, open: false }))
  const runConfirm = async () => { closeConfirm(); await confirmDialog.onConfirm() }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, height: '100%', overflow: 'hidden' }}>
      <Box sx={{ borderBottom: 1, borderColor: 'divider', px: 2, flexShrink: 0 }}>
        <Tabs value={tab} onChange={(_, v) => handleTabChange(_, v as SubTab)}>
          {TAB_ORDER.map(key => (
            <Tab key={key} value={key} label={TAB_LABELS[key]} sx={{ minHeight: 48 }} />
          ))}
        </Tabs>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {tab === 'workflows' && (
          <Box sx={{ flex: 1, minHeight: 0 }}>
            <WorkflowBuilder />
          </Box>
        )}
        {tab === 'agents' && (
          <Box sx={{ p: 2, flex: 1, overflow: 'auto' }}>
            <AgentBuilderTab
              onMessage={setMessage}
              showConfirm={showConfirm}
            />
          </Box>
        )}
        {tab === 'skills' && (
          <Box sx={{ p: 2, flex: 1, overflow: 'auto' }}>
            <SkillsTab />
          </Box>
        )}
      </Box>

      <Snackbar open={!!message} autoHideDuration={4000} onClose={() => setMessage(null)}>
        {message ? (
          <Alert severity={message.type} onClose={() => setMessage(null)}>
            {message.text}
          </Alert>
        ) : undefined}
      </Snackbar>

      <Dialog open={confirmDialog.open} onClose={closeConfirm}>
        <DialogTitle>{confirmDialog.title}</DialogTitle>
        <DialogContent>
          <DialogContentText>{confirmDialog.msg}</DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={closeConfirm}>Cancel</Button>
          <Button onClick={runConfirm} variant="contained">Confirm</Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
