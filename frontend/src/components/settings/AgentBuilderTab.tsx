import { useCallback, useEffect, useMemo, useState } from 'react'
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
  ContentCopy as ForkIcon,
  SmartToy as AgentIcon,
  Lock as LockIcon,
} from '@mui/icons-material'
import { agentsApi, type CustomAgent, type AgentSummary } from '../../services/api'
import AgentBuilderDialog from './AgentBuilderDialog'

interface Props {
  onMessage: (msg: { type: 'success' | 'error'; text: string }) => void
  showConfirm: (title: string, msg: string, onConfirm: () => void) => void
}

type Row = AgentSummary & {
  is_builtin: boolean
  // Custom rows carry the extras; built-ins leave these undefined.
  forked_from?: string | null
  recommended_tools?: string[]
  updated_at?: string
}

const CUSTOM_PREFIX = 'custom-'

export default function AgentBuilderTab({ onMessage, showConfirm }: Props) {
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      // Pull both streams: the unified list for display, and the custom
      // rows so we can surface updated_at + recommended_tools counts.
      const [unifiedRes, customRes] = await Promise.all([
        agentsApi.listAgents(),
        agentsApi.listCustom().catch(() => ({ data: { agents: [] as CustomAgent[] } })),
      ])
      const unified: AgentSummary[] = unifiedRes.data?.agents || []
      const customs: CustomAgent[] = customRes.data?.agents || []
      const customById = new Map(customs.map((c) => [c.id, c]))
      const merged: Row[] = unified.map((a) => {
        const is_builtin = !a.id.startsWith(CUSTOM_PREFIX)
        const custom = customById.get(a.id)
        return {
          ...a,
          is_builtin,
          forked_from: custom?.forked_from ?? null,
          recommended_tools: custom?.recommended_tools ?? undefined,
          updated_at: custom?.updated_at,
        }
      })
      setRows(merged)
    } catch (err: any) {
      onMessage({
        type: 'error',
        text: `Failed to load agents: ${err?.response?.data?.detail || err?.message || 'unknown error'}`,
      })
    } finally {
      setLoading(false)
    }
  }, [onMessage])

  useEffect(() => {
    load()
  }, [load])

  const builtins = useMemo(() => rows.filter((r) => r.is_builtin), [rows])
  const customs = useMemo(() => rows.filter((r) => !r.is_builtin), [rows])

  const handleNew = () => {
    setEditingId(null)
    setDialogOpen(true)
  }

  const handleEdit = (id: string) => {
    setEditingId(id)
    setDialogOpen(true)
  }

  const handleFork = async (row: Row) => {
    setBusyId(row.id)
    try {
      const res = await agentsApi.forkAgent(row.id)
      const newId: string = res.data?.id
      onMessage({ type: 'success', text: `Forked "${row.name}" → opening editor` })
      setTimeout(() => onMessage({ type: 'success', text: '' }), 2500)
      await load()
      // Drop the user straight into the editor so the fork feels like a
      // "customize this template" flow, not a silent copy.
      if (newId) {
        setEditingId(newId)
        setDialogOpen(true)
      }
    } catch (err: any) {
      onMessage({
        type: 'error',
        text: `Fork failed: ${err?.response?.data?.detail || err?.message || 'unknown error'}`,
      })
    } finally {
      setBusyId(null)
    }
  }

  const handleDelete = (row: Row) => {
    showConfirm(
      'Delete custom agent',
      `Delete ${row.name}? This cannot be undone. The built-in it was forked from (if any) is unaffected.`,
      async () => {
        try {
          await agentsApi.deleteCustom(row.id)
          onMessage({ type: 'success', text: `Deleted ${row.name}` })
          setTimeout(() => onMessage({ type: 'success', text: '' }), 3000)
          await load()
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
    if (saved) load()
  }

  const renderAvatar = (row: Row) => (
    <Box
      sx={{
        width: 28,
        height: 28,
        borderRadius: '50%',
        bgcolor: row.color || '#888',
        color: '#fff',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: '0.8rem',
        fontWeight: 700,
        flexShrink: 0,
      }}
    >
      {row.icon || (row.is_builtin ? 'B' : 'C')}
    </Box>
  )

  const renderRow = (row: Row) => (
    <TableRow key={row.id} hover>
      <TableCell>
        <Stack direction="row" spacing={1.25} alignItems="center">
          {renderAvatar(row)}
          <Box>
            <Stack direction="row" spacing={0.75} alignItems="center">
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {row.name}
              </Typography>
              {row.is_builtin && (
                <Chip
                  size="small"
                  label="Template"
                  icon={<LockIcon sx={{ fontSize: 13 }} />}
                  sx={{ height: 20, fontSize: '0.7rem' }}
                />
              )}
              {row.forked_from && (
                <Tooltip title={`Forked from ${row.forked_from}`} arrow>
                  <Chip
                    size="small"
                    label={`⎇ ${row.forked_from}`}
                    variant="outlined"
                    sx={{ height: 20, fontSize: '0.7rem' }}
                  />
                </Tooltip>
              )}
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
              {row.id}
            </Typography>
          </Box>
        </Stack>
      </TableCell>
      <TableCell>{row.specialization || '—'}</TableCell>
      <TableCell>
        {row.recommended_tools ? (
          <Chip size="small" label={`${row.recommended_tools.length} tool(s)`} />
        ) : (
          <Typography variant="caption" color="text.disabled">
            —
          </Typography>
        )}
      </TableCell>
      <TableCell>
        <Typography variant="caption" color="text.secondary">
          {row.updated_at ? new Date(row.updated_at).toLocaleString() : '—'}
        </Typography>
      </TableCell>
      <TableCell align="right">
        {row.is_builtin ? (
          <Tooltip title="Fork this template into an editable custom agent" arrow>
            <span>
              <IconButton
                size="small"
                onClick={() => handleFork(row)}
                disabled={busyId === row.id}
              >
                {busyId === row.id ? <CircularProgress size={16} /> : <ForkIcon fontSize="small" />}
              </IconButton>
            </span>
          </Tooltip>
        ) : (
          <>
            <Tooltip title="Edit">
              <IconButton size="small" onClick={() => handleEdit(row.id)}>
                <EditIcon fontSize="small" />
              </IconButton>
            </Tooltip>
            <Tooltip title="Fork into a new copy">
              <span>
                <IconButton
                  size="small"
                  onClick={() => handleFork(row)}
                  disabled={busyId === row.id}
                >
                  {busyId === row.id ? <CircularProgress size={16} /> : <ForkIcon fontSize="small" />}
                </IconButton>
              </span>
            </Tooltip>
            <Tooltip title="Delete">
              <IconButton size="small" onClick={() => handleDelete(row)}>
                <DeleteIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </>
        )}
      </TableCell>
    </TableRow>
  )

  return (
    <Box>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 2 }}>
        <Box>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            SOC Agents
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Built-in agents are read-only templates. Fork one to create an
            editable custom copy, or start from scratch with "New Agent".
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
      ) : rows.length === 0 ? (
        <Paper variant="outlined" sx={{ p: 4, textAlign: 'center' }}>
          <AgentIcon sx={{ fontSize: 40, color: 'text.disabled', mb: 1 }} />
          <Typography variant="body1" sx={{ mb: 0.5 }}>
            No agents available
          </Typography>
        </Paper>
      ) : (
        <Stack spacing={3}>
          {customs.length > 0 && (
            <Box>
              <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5 }}>
                Your agents ({customs.length})
              </Typography>
              <TableContainer component={Paper} variant="outlined" sx={{ mt: 0.5 }}>
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
                  <TableBody>{customs.map(renderRow)}</TableBody>
                </Table>
              </TableContainer>
            </Box>
          )}

          <Box>
            <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5 }}>
              Built-in templates ({builtins.length})
            </Typography>
            <TableContainer component={Paper} variant="outlined" sx={{ mt: 0.5 }}>
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
                <TableBody>{builtins.map(renderRow)}</TableBody>
              </Table>
            </TableContainer>
          </Box>
        </Stack>
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
