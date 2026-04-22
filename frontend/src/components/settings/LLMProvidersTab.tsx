import { useState, useEffect } from 'react'
import {
  Box,
  Typography,
  Button,
  Chip,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  IconButton,
  Tooltip,
  CircularProgress,
} from '@mui/material'
import {
  Add as AddIcon,
  Science as TestIcon,
  Edit as EditIcon,
  Delete as DeleteIcon,
  Star as DefaultIcon,
  StarBorder as NotDefaultIcon,
} from '@mui/icons-material'
import { llmProviderApi, LLMProvider } from '../../services/api'
import LLMProviderDialog from './LLMProviderDialog'

interface Props {
  setMessage: (m: { type: 'success' | 'error'; text: string } | null) => void
}

export default function LLMProvidersTab({ setMessage }: Props) {
  const [providers, setProviders] = useState<LLMProvider[]>([])
  const [loading, setLoading] = useState(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<LLMProvider | null>(null)
  const [testingId, setTestingId] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const resp = await llmProviderApi.list()
      setProviders(resp.data)
    } catch (e: any) {
      setMessage({ type: 'error', text: e?.response?.data?.detail || 'Failed to load providers' })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const handleTest = async (id: string) => {
    setTestingId(id)
    try {
      const resp = await llmProviderApi.test(id)
      if (resp.data.success) {
        setMessage({ type: 'success', text: `Connection OK for ${id}` })
      } else {
        setMessage({ type: 'error', text: `Test failed: ${resp.data.error || 'unknown error'}` })
      }
      await load()
    } catch (e: any) {
      setMessage({ type: 'error', text: e?.response?.data?.detail || 'Test request failed' })
    } finally {
      setTestingId(null)
    }
  }

  const handleDelete = async (id: string) => {
    if (!window.confirm(`Delete provider "${id}"? This also removes its stored API key.`)) return
    try {
      await llmProviderApi.remove(id)
      setMessage({ type: 'success', text: `Deleted ${id}` })
      await load()
    } catch (e: any) {
      setMessage({ type: 'error', text: e?.response?.data?.detail || 'Delete failed' })
    }
  }

  const handleSetDefault = async (id: string) => {
    try {
      await llmProviderApi.setDefault(id)
      setMessage({ type: 'success', text: `Default set to ${id}` })
      await load()
    } catch (e: any) {
      setMessage({ type: 'error', text: e?.response?.data?.detail || 'Failed to set default' })
    }
  }

  const statusChip = (p: LLMProvider) => {
    if (!p.is_active) return <Chip size="small" label="Inactive" />
    if (p.last_test_success === true) return <Chip size="small" color="success" label="Active" />
    if (p.last_test_success === false) return <Chip size="small" color="error" label="Error" />
    return <Chip size="small" color="default" label="Untested" />
  }

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', mb: 2 }}>
        <Typography variant="subtitle1" sx={{ fontWeight: 600, flexGrow: 1 }}>
          LLM Providers
        </Typography>
        <Button
          startIcon={<AddIcon />}
          variant="contained"
          onClick={() => {
            setEditing(null)
            setDialogOpen(true)
          }}
        >
          Add Provider
        </Button>
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
        Configure additional Anthropic, OpenAI, or Ollama providers. All traffic is routed through
        the Bifrost gateway — Anthropic calls hit the /anthropic passthrough so extended thinking and
        prompt caching round-trip unchanged.
      </Typography>

      {loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
          <CircularProgress size={24} />
        </Box>
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 600 }}>Name</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>Type</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>Model</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>Status</TableCell>
                <TableCell sx={{ fontWeight: 600 }}>Default</TableCell>
                <TableCell sx={{ fontWeight: 600 }} align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {providers.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} align="center" sx={{ py: 3, color: 'text.secondary' }}>
                    No providers configured.
                  </TableCell>
                </TableRow>
              ) : (
                providers.map((p) => (
                  <TableRow key={p.provider_id} hover>
                    <TableCell>
                      <Typography variant="body2" sx={{ fontWeight: 500 }}>{p.name}</Typography>
                      <Typography variant="caption" color="text.secondary">{p.provider_id}</Typography>
                    </TableCell>
                    <TableCell>
                      <Chip size="small" label={p.provider_type} />
                    </TableCell>
                    <TableCell>
                      <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                        {p.default_model}
                      </Typography>
                    </TableCell>
                    <TableCell>{statusChip(p)}</TableCell>
                    <TableCell>
                      <Tooltip title={p.is_default ? 'Default for this provider type' : 'Set as default'}>
                        <IconButton
                          size="small"
                          onClick={() => !p.is_default && handleSetDefault(p.provider_id)}
                          color={p.is_default ? 'primary' : 'default'}
                        >
                          {p.is_default ? <DefaultIcon fontSize="small" /> : <NotDefaultIcon fontSize="small" />}
                        </IconButton>
                      </Tooltip>
                    </TableCell>
                    <TableCell align="right">
                      <Tooltip title="Test connection">
                        <span>
                          <IconButton
                            size="small"
                            disabled={testingId === p.provider_id}
                            onClick={() => handleTest(p.provider_id)}
                          >
                            {testingId === p.provider_id
                              ? <CircularProgress size={16} />
                              : <TestIcon fontSize="small" />}
                          </IconButton>
                        </span>
                      </Tooltip>
                      <Tooltip title="Edit">
                        <IconButton
                          size="small"
                          onClick={() => {
                            setEditing(p)
                            setDialogOpen(true)
                          }}
                        >
                          <EditIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                      <Tooltip title="Delete">
                        <IconButton
                          size="small"
                          onClick={() => handleDelete(p.provider_id)}
                        >
                          <DeleteIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {dialogOpen && (
        <LLMProviderDialog
          existing={editing}
          onClose={() => {
            setDialogOpen(false)
            setEditing(null)
          }}
          onSaved={async () => {
            setDialogOpen(false)
            setEditing(null)
            setMessage({ type: 'success', text: 'Provider saved' })
            await load()
          }}
          onError={(msg) => setMessage({ type: 'error', text: msg })}
        />
      )}
    </Box>
  )
}
