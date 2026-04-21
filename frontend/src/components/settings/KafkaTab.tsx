import { useState, useEffect, useRef } from 'react'
import {
  Box,
  Typography,
  TextField,
  Button,
  Switch,
  FormControlLabel,
  Divider,
  Chip,
  Card,
  CardContent,
  Alert,
  CircularProgress,
  Grid,
  MenuItem,
  Stack,
} from '@mui/material'
import {
  Save as SaveIcon,
  Stream as StreamIcon,
  Add as AddIcon,
  Close as CloseIcon,
} from '@mui/icons-material'
import { kafkaApi } from '../../services/api'

interface KafkaConfig {
  enabled: boolean
  bootstrap_servers: string
  consumer_group: string
  topics: string[]
  auto_offset_reset: string
  security_protocol: string
  sasl_mechanism: string | null
  sasl_username: string | null
  max_poll_records: number
  session_timeout_ms: number
}

interface KafkaStats {
  connected: boolean
  messages_consumed: number
  messages_enqueued: number
  duplicates_skipped: number
  decode_errors: number
  missing_id_errors: number
  last_message_at: string | null
  last_error: string | null
  last_error_at: string | null
  topics: string[]
  consumer_group: string
}

const DEFAULTS: KafkaConfig = {
  enabled: false,
  bootstrap_servers: 'localhost:9092',
  consumer_group: 'vigil-soc',
  topics: [],
  auto_offset_reset: 'latest',
  security_protocol: 'PLAINTEXT',
  sasl_mechanism: null,
  sasl_username: null,
  max_poll_records: 500,
  session_timeout_ms: 30000,
}

const OFFSET_RESETS = ['latest', 'earliest']
const SECURITY_PROTOCOLS = ['PLAINTEXT', 'SSL', 'SASL_PLAINTEXT', 'SASL_SSL']
const SASL_MECHANISMS = ['', 'PLAIN', 'SCRAM-SHA-256', 'SCRAM-SHA-512']

interface Props {
  onMessage: (msg: { type: 'success' | 'error'; text: string }) => void
}

export default function KafkaTab({ onMessage }: Props) {
  const [config, setConfig] = useState<KafkaConfig>(DEFAULTS)
  const [stats, setStats] = useState<KafkaStats | null>(null)
  const [daemonReachable, setDaemonReachable] = useState<boolean>(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [topicInput, setTopicInput] = useState('')
  const statusTimer = useRef<number | null>(null)

  const loadStatus = async () => {
    try {
      const res = await kafkaApi.getStatus()
      const data = res.data
      setDaemonReachable(!!data.daemon_reachable)
      setStats(data.stats || null)
      if (data.config) {
        setConfig({ ...DEFAULTS, ...data.config })
      }
    } catch {
      setDaemonReachable(false)
    }
  }

  useEffect(() => {
    (async () => {
      try {
        const res = await kafkaApi.getConfig()
        setConfig({ ...DEFAULTS, ...res.data })
      } catch {
        /* use defaults */
      }
      await loadStatus()
      setLoading(false)
    })()
    statusTimer.current = window.setInterval(loadStatus, 5000)
    return () => {
      if (statusTimer.current !== null) {
        window.clearInterval(statusTimer.current)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSave = async () => {
    setSaving(true)
    try {
      await kafkaApi.setConfig({
        ...config,
        sasl_mechanism: config.sasl_mechanism || null,
        sasl_username: config.sasl_username || null,
      })
      onMessage({ type: 'success', text: 'Kafka settings saved' })
      await loadStatus()
    } catch (e: any) {
      onMessage({
        type: 'error',
        text: `Failed to save Kafka settings: ${e?.response?.data?.detail || e?.message || e}`,
      })
    } finally {
      setSaving(false)
      setTimeout(() => onMessage({ type: 'success', text: '' }), 3000)
    }
  }

  const addTopic = () => {
    const t = topicInput.trim()
    if (!t) return
    if (config.topics.includes(t)) {
      setTopicInput('')
      return
    }
    setConfig({ ...config, topics: [...config.topics, t] })
    setTopicInput('')
  }

  const removeTopic = (topic: string) => {
    setConfig({ ...config, topics: config.topics.filter((x) => x !== topic) })
  }

  if (loading) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 2 }}>
        <StreamIcon />
        <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
          Kafka Ingestion
        </Typography>
      </Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Stream security findings from Kafka topics into Vigil. Messages
        must be JSON-encoded finding objects containing at minimum a
        <code> finding_id</code> field. Secrets (SASL password, SSL CA
        path) must be set via environment variables — they are not
        editable here.
      </Typography>

      {!daemonReachable && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Daemon health endpoint unreachable — live stats unavailable.
          Changes will apply once the daemon can read the updated config.
        </Alert>
      )}

      <Alert
        severity={stats?.connected ? 'success' : config.enabled ? 'warning' : 'info'}
        sx={{ mb: 3 }}
      >
        Consumer is{' '}
        <strong>
          {stats?.connected
            ? 'CONNECTED'
            : config.enabled
              ? 'ENABLED (not yet connected)'
              : 'DISABLED'}
        </strong>
        {stats && ` — ${stats.messages_consumed} consumed, ${stats.messages_enqueued} enqueued, ${stats.duplicates_skipped} dupes skipped`}
        {stats?.last_message_at && ` | last message: ${new Date(stats.last_message_at).toLocaleString()}`}
        {stats?.last_error && (
          <Box sx={{ mt: 1, fontSize: '0.85rem' }}>
            Last error: {stats.last_error}
          </Box>
        )}
      </Alert>

      <Card sx={{ mb: 3 }}>
        <CardContent>
          <FormControlLabel
            control={
              <Switch
                checked={config.enabled}
                onChange={(e) => setConfig({ ...config, enabled: e.target.checked })}
              />
            }
            label={
              <Box>
                <Typography variant="body1" sx={{ fontWeight: 500 }}>
                  Enable Kafka consumer
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  The daemon re-reads this flag every few seconds — no restart needed.
                </Typography>
              </Box>
            }
          />
        </CardContent>
      </Card>

      <Typography variant="subtitle2" sx={{ mb: 2, fontWeight: 600 }}>
        Connection
      </Typography>
      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} md={6}>
          <TextField
            fullWidth
            size="small"
            label="Bootstrap servers"
            value={config.bootstrap_servers}
            onChange={(e) => setConfig({ ...config, bootstrap_servers: e.target.value })}
            helperText="Comma-separated host:port list"
          />
        </Grid>
        <Grid item xs={12} md={6}>
          <TextField
            fullWidth
            size="small"
            label="Consumer group"
            value={config.consumer_group}
            onChange={(e) => setConfig({ ...config, consumer_group: e.target.value })}
          />
        </Grid>
        <Grid item xs={12} md={6}>
          <TextField
            select
            fullWidth
            size="small"
            label="Auto offset reset"
            value={config.auto_offset_reset}
            onChange={(e) => setConfig({ ...config, auto_offset_reset: e.target.value })}
          >
            {OFFSET_RESETS.map((o) => (
              <MenuItem key={o} value={o}>{o}</MenuItem>
            ))}
          </TextField>
        </Grid>
        <Grid item xs={12} md={6}>
          <TextField
            select
            fullWidth
            size="small"
            label="Security protocol"
            value={config.security_protocol}
            onChange={(e) => setConfig({ ...config, security_protocol: e.target.value })}
          >
            {SECURITY_PROTOCOLS.map((p) => (
              <MenuItem key={p} value={p}>{p}</MenuItem>
            ))}
          </TextField>
        </Grid>
      </Grid>

      <Typography variant="subtitle2" sx={{ mb: 2, fontWeight: 600 }}>
        Topics
      </Typography>
      <Stack direction="row" spacing={1} sx={{ mb: 1 }} alignItems="center">
        <TextField
          size="small"
          label="Add topic"
          value={topicInput}
          onChange={(e) => setTopicInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              addTopic()
            }
          }}
          sx={{ flex: 1 }}
        />
        <Button onClick={addTopic} startIcon={<AddIcon />} variant="outlined" size="small">
          Add
        </Button>
      </Stack>
      <Box sx={{ mb: 3 }}>
        {config.topics.length === 0 ? (
          <Typography variant="caption" color="text.secondary">
            No topics configured — the consumer will not start until you add at least one.
          </Typography>
        ) : (
          config.topics.map((t) => (
            <Chip
              key={t}
              label={t}
              onDelete={() => removeTopic(t)}
              deleteIcon={<CloseIcon />}
              sx={{ mr: 1, mb: 1 }}
            />
          ))
        )}
      </Box>

      <Typography variant="subtitle2" sx={{ mb: 2, fontWeight: 600 }}>
        Authentication (SASL)
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
        Passwords and SSL CA paths must be set via env vars
        (<code>KAFKA_SASL_PASSWORD</code>, <code>KAFKA_SSL_CA_LOCATION</code>).
      </Typography>
      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} md={6}>
          <TextField
            select
            fullWidth
            size="small"
            label="SASL mechanism"
            value={config.sasl_mechanism ?? ''}
            onChange={(e) =>
              setConfig({ ...config, sasl_mechanism: e.target.value || null })
            }
          >
            {SASL_MECHANISMS.map((m) => (
              <MenuItem key={m || 'none'} value={m}>
                {m || '(none)'}
              </MenuItem>
            ))}
          </TextField>
        </Grid>
        <Grid item xs={12} md={6}>
          <TextField
            fullWidth
            size="small"
            label="SASL username"
            value={config.sasl_username ?? ''}
            onChange={(e) =>
              setConfig({ ...config, sasl_username: e.target.value || null })
            }
          />
        </Grid>
      </Grid>

      <Divider sx={{ mb: 3 }} />

      <Typography variant="subtitle2" sx={{ mb: 2, fontWeight: 600 }}>
        Advanced
      </Typography>
      <Grid container spacing={2} sx={{ mb: 3 }}>
        <Grid item xs={12} md={6}>
          <TextField
            fullWidth
            type="number"
            size="small"
            label="Max poll records"
            value={config.max_poll_records}
            onChange={(e) =>
              setConfig({ ...config, max_poll_records: Number(e.target.value) })
            }
          />
        </Grid>
        <Grid item xs={12} md={6}>
          <TextField
            fullWidth
            type="number"
            size="small"
            label="Session timeout (ms)"
            value={config.session_timeout_ms}
            onChange={(e) =>
              setConfig({ ...config, session_timeout_ms: Number(e.target.value) })
            }
          />
        </Grid>
      </Grid>

      <Stack direction="row" spacing={2}>
        <Button
          variant="contained"
          startIcon={<SaveIcon />}
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? 'Saving…' : 'Save Kafka settings'}
        </Button>
      </Stack>
    </Box>
  )
}
