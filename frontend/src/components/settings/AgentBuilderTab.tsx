import { useCallback, useEffect, useState } from 'react'
import {
  Box,
  Typography,
  Button,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  IconButton,
  Chip,
  CircularProgress,
  Tooltip,
  Stack,
} from '@mui/material'
import {
  Add as AddIcon,
  Edit as EditIcon,
  Delete as DeleteIcon,
  SmartToy as AgentIcon,
} from '@mui/icons-material'
import { agentsApi, type CustomAgent } from '../../services/api'
import AgentBuilderDialog from './AgentBuilderDialog'

interface Props {
  onMessage: (msg: { type: 'success' | 'error'; text: string }) => void
  showConfirm: (title: string, msg: string, onConfirm: () => void) => void
}

export default function AgentBuilderTab({ onMessage, showConfirm }: Props) {
  const [agents, setAgents] = useState<CustomAgent[]>([])
  const [loading, setLoading] = useState(true)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)

  const loadAgents = useCallback(async () => {
    setLoading(true)
    try {
      const res = await agentsApi.listCustom()
      setAgents(res.data.agents || [])
    } catch (err: any) {
      onMessage({
        type: 'error',
        text: `Failed to load custom agents: ${err?.response?.data?.detail || err?.message || 'unknown error'}`,
      })
    } finally {
      setLoading(false)
    }
  }, [onMessage])

  useEffect(() => {
    loadAgents()
  }, [loadAgents])

  const handleNew = () => {
    setEditingId(null)
    setDialogOpen(true)
  }

  const handleEdit = (id: string) => {
    setEditingId(id)
    setDialogOpen(true)
  }

  const handleDelete = (agent: CustomAgent) => {
    showConfirm(
      'Delete custom agent',
      `Delete ${agent.name}? This cannot be undone.`,
      async () => {
        try {
          await agentsApi.deleteCustom(agent.id)
          onMessage({ type: 'success', text: `Deleted ${agent.name}` })
          setTimeout(() => onMessage({ type: 'success', text: '' }), 3000)
          await loadAgents()
        } catch (err: any) {
          onMessage({
            type: 'error',
            text: `Delete failed: ${err?.response?.data?.detail || err?.message || 'unknown error'}`,
          })
        }
      }
    )
  }

  const handleDialogClose = (saved: boolean) => {
    setDialogOpen(false)
    setEditingId(null)
    if (saved) {
      loadAgents()
    }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
        <Box>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            SOC Agents
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Create custom specialized agents in addition to the 13 built-in SOC agents.
          </Typography>
        </Box>
        <Button variant="contained" startIcon={<AddIcon />} onClick={handleNew}>
          New Agent
        </Button>
      </Stack>

      {loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}>
          <CircularProgress size={28} />
        </Box>
      ) : agents.length === 0 ? (
        <Paper variant="outlined" sx={{ p: 4, textAlign: 'center' }}>
          <AgentIcon sx={{ fontSize: 40, color: 'text.disabled', mb: 1 }} />
          <Typography variant="body1" sx={{ mb: 0.5 }}>
            No custom agents yet
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Click "New Agent" to create one. Custom agents appear in the Skills page alongside built-ins.
          </Typography>
        </Paper>
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Name</TableCell>
                <TableCell>Specialization</TableCell>
                <TableCell>Tools</TableCell>
                <TableCell>Updated</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {agents.map((a) => (
                <TableRow key={a.id} hover>
                  <TableCell>
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Box
                        sx={{
                          width: 24,
                          height: 24,
                          borderRadius: '50%',
                          bgcolor: a.color || '#888',
                          color: '#fff',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          fontSize: '0.75rem',
                          fontWeight: 700,
                        }}
                      >
                        {a.icon || 'C'}
                      </Box>
                      <Box>
                        <Typography variant="body2" sx={{ fontWeight: 600 }}>
                          {a.name}
                        </Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
                          {a.id}
                        </Typography>
                      </Box>
                    </Stack>
                  </TableCell>
                  <TableCell>{a.specialization || '—'}</TableCell>
                  <TableCell>
                    <Chip size="small" label={`${(a.recommended_tools || []).length} tool(s)`} />
                  </TableCell>
                  <TableCell>
                    <Typography variant="caption" color="text.secondary">
                      {a.updated_at ? new Date(a.updated_at).toLocaleString() : '—'}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">
                    <Tooltip title="Edit">
                      <IconButton size="small" onClick={() => handleEdit(a.id)}>
                        <EditIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title="Delete">
                      <IconButton size="small" onClick={() => handleDelete(a)}>
                        <DeleteIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <AgentBuilderDialog
        open={dialogOpen}
        agentId={editingId}
        onClose={handleDialogClose}
        onMessage={onMessage}
      />
    </Box>
  )
}
