import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  FormControl,
  FormControlLabel,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Switch,
  TextField,
  Typography,
} from '@mui/material'
import { Save as SaveIcon } from '@mui/icons-material'

import { configApi } from '../../services/api'

type ProxyType = 'none' | 'pgbouncer' | 'ssh_tunnel'

interface FormState {
  proxy_type: ProxyType
  proxy_host: string
  proxy_port: number
  proxy_username: string
  proxy_password: string
  ssh_private_key_path: string
  ssh_key_passphrase: string
  verify_proxy_tls: boolean
}

const EMPTY_FORM: FormState = {
  proxy_type: 'none',
  proxy_host: '',
  proxy_port: 0,
  proxy_username: '',
  proxy_password: '',
  ssh_private_key_path: '',
  ssh_key_passphrase: '',
  verify_proxy_tls: true,
}

interface Props {
  setMessage: (m: { type: 'success' | 'error'; text: string } | null) => void
}

// The platform's own metadata Postgres can be fronted by PgBouncer or
// reached via an SSH tunnel. HTTP/SOCKS aren't offered: the Postgres
// wire protocol isn't proxy-aware in psycopg2/asyncpg, so a SOCKS
// "proxy" on the platform DB would silently fail.
export default function PlatformDatabaseTab({ setMessage }: Props) {
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [hasPassword, setHasPassword] = useState(false)
  const [hasPassphrase, setHasPassphrase] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const { data } = await configApi.getPlatformDatabase()
        if (cancelled) return
        const proxyType = (
          ['none', 'pgbouncer', 'ssh_tunnel'].includes(data.proxy_type)
            ? data.proxy_type
            : 'none'
        ) as ProxyType
        setForm({
          proxy_type: proxyType,
          proxy_host: data.proxy_host || '',
          proxy_port: Number(data.proxy_port) || 0,
          proxy_username: data.proxy_username || '',
          proxy_password: '',
          ssh_private_key_path: data.ssh_private_key_path || '',
          ssh_key_passphrase: '',
          verify_proxy_tls: data.verify_proxy_tls ?? true,
        })
        setHasPassword(Boolean(data.has_proxy_password))
        setHasPassphrase(Boolean(data.has_ssh_key_passphrase))
      } catch (err: any) {
        setMessage({
          type: 'error',
          text: `Failed to load platform DB proxy config: ${err?.message || err}`,
        })
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [setMessage])

  const handleSave = async () => {
    setSaving(true)
    try {
      await configApi.setPlatformDatabase({
        proxy_type: form.proxy_type,
        proxy_host: form.proxy_host,
        proxy_port: form.proxy_port || 0,
        proxy_username: form.proxy_username,
        proxy_password: form.proxy_password,
        ssh_private_key_path: form.ssh_private_key_path,
        ssh_key_passphrase: form.ssh_key_passphrase,
        verify_proxy_tls: form.verify_proxy_tls,
      })
      // Clear secret inputs so a future save doesn't accidentally
      // overwrite stored values with stale UI state.
      setForm((prev) => ({ ...prev, proxy_password: '', ssh_key_passphrase: '' }))
      if (form.proxy_password) setHasPassword(true)
      if (form.ssh_key_passphrase) setHasPassphrase(true)
      setMessage({
        type: 'success',
        text: 'Platform DB proxy saved. Restart the backend for changes to take effect.',
      })
    } catch (err: any) {
      setMessage({
        type: 'error',
        text: `Failed to save platform DB proxy: ${err?.message || err}`,
      })
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}>
        <CircularProgress />
      </Box>
    )
  }

  const sshFieldsRelevant = form.proxy_type === 'ssh_tunnel'

  return (
    <Box sx={{ maxWidth: 760 }}>
      <Typography variant="h6" sx={{ mb: 1 }}>
        Platform Database Proxy
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Optional intermediate hop in front of Vigil's own metadata
        Postgres (PgBouncer pooler or SSH tunnel to a private bastion).
        Credentials are kept in the encrypted secrets store, never the
        DB itself. HTTP/SOCKS proxies are not offered for the platform
        DB because the Postgres wire protocol is not proxy-aware.
      </Typography>

      <Alert severity="warning" sx={{ mb: 3 }}>
        Changes here take effect after the backend is restarted. The
        live engine cannot be hot-swapped safely.
      </Alert>

      <Paper sx={{ p: 3 }} variant="outlined">
        <FormControl fullWidth margin="normal">
          <InputLabel>Proxy Type</InputLabel>
          <Select
            value={form.proxy_type}
            label="Proxy Type"
            onChange={(e) =>
              setForm({ ...form, proxy_type: e.target.value as ProxyType })
            }
          >
            <MenuItem value="none">None (direct connection)</MenuItem>
            <MenuItem value="pgbouncer">PgBouncer</MenuItem>
            <MenuItem value="ssh_tunnel">SSH tunnel</MenuItem>
          </Select>
        </FormControl>

        {form.proxy_type !== 'none' && (
          <>
            <TextField
              fullWidth
              margin="normal"
              label="Proxy Host"
              value={form.proxy_host}
              onChange={(e) => setForm({ ...form, proxy_host: e.target.value })}
              placeholder={sshFieldsRelevant ? 'bastion.internal' : 'pgbouncer.internal'}
              required
            />
            <TextField
              fullWidth
              margin="normal"
              type="number"
              label="Proxy Port"
              value={form.proxy_port || ''}
              onChange={(e) =>
                setForm({ ...form, proxy_port: parseInt(e.target.value) || 0 })
              }
              placeholder={sshFieldsRelevant ? '22' : '6432'}
              required
            />
            <TextField
              fullWidth
              margin="normal"
              label={sshFieldsRelevant ? 'SSH Username' : 'Proxy Username'}
              value={form.proxy_username}
              onChange={(e) =>
                setForm({ ...form, proxy_username: e.target.value })
              }
            />
            <TextField
              fullWidth
              margin="normal"
              type="password"
              label="Proxy Password"
              value={form.proxy_password}
              onChange={(e) =>
                setForm({ ...form, proxy_password: e.target.value })
              }
              helperText={
                hasPassword
                  ? 'A password is currently stored. Leave blank to keep it; type a new value to overwrite.'
                  : 'Stored in the encrypted secrets store.'
              }
            />

            {sshFieldsRelevant && (
              <>
                <TextField
                  fullWidth
                  margin="normal"
                  label="SSH Private Key Path"
                  value={form.ssh_private_key_path}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      ssh_private_key_path: e.target.value,
                    })
                  }
                  placeholder="/home/vigil/.ssh/id_ed25519"
                  helperText="Absolute path on the backend host."
                />
                <TextField
                  fullWidth
                  margin="normal"
                  type="password"
                  label="SSH Key Passphrase"
                  value={form.ssh_key_passphrase}
                  onChange={(e) =>
                    setForm({ ...form, ssh_key_passphrase: e.target.value })
                  }
                  helperText={
                    hasPassphrase
                      ? 'A passphrase is currently stored. Leave blank to keep it.'
                      : 'Required only with an encrypted private key.'
                  }
                />
              </>
            )}

            <FormControlLabel
              sx={{ mt: 2 }}
              control={
                <Switch
                  checked={form.verify_proxy_tls}
                  onChange={(e) =>
                    setForm({ ...form, verify_proxy_tls: e.target.checked })
                  }
                />
              }
              label="Verify proxy TLS"
            />
          </>
        )}

        <Box sx={{ mt: 3 }}>
          <Button
            variant="contained"
            startIcon={<SaveIcon />}
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? 'Saving...' : 'Save Configuration'}
          </Button>
        </Box>
      </Paper>
    </Box>
  )
}
