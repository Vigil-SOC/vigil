import { useState, useEffect, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Box,
  Typography,
  Tabs,
  Tab,
  TextField,
  Button,
  Alert,
  Switch,
  FormControlLabel,
  FormGroup,
  Divider,
  Chip,
  Grid,
  Card,
  CardContent,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  DialogContentText,
  Accordion,
  AccordionSummary,
  AccordionDetails,
  InputAdornment,
  alpha,
  useTheme,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Checkbox,
  CircularProgress,
  LinearProgress,
  ToggleButtonGroup,
  ToggleButton,
} from '@mui/material'
import { useNotifications } from '../contexts/NotificationContext'
import { notificationService } from '../services/notifications'
import {
  Save as SaveIcon,
  Refresh as RefreshIcon,
  Add as AddIcon,
  CheckCircle as CheckIcon,
  PlayArrow as StartIcon,
  Stop as StopIcon,
  OpenInNew as OpenInNewIcon,
  HelpOutline as HelpIcon,
  ExpandMore as ExpandMoreIcon,
  Search as SearchIcon,
  CloudDownload as CloudDownloadIcon,
  FolderOpen as FolderOpenIcon,
  UploadFile as UploadFileIcon,
  DeleteSweep as DeleteSweepIcon,
} from '@mui/icons-material'
import { configApi, mcpApi, storageApi, localServicesApi, ingestionApi, findingsApi } from '../services/api'
import IntegrationWizard, { IntegrationMetadata } from '../components/settings/IntegrationWizard'
import CustomIntegrationBuilder from '../components/settings/CustomIntegrationBuilder'
import { getAllIntegrations, loadCustomIntegrations } from '../config/integrations'
import UserManagementTab from '../components/settings/UserManagementTab'
import AgentBuilderTab from '../components/settings/AgentBuilderTab'
import DetectionRulesTab from '../components/settings/DetectionRulesTab'
import AutoInvestigateTab from '../components/settings/AutoInvestigateTab'
import SkillsTab from '../components/settings/SkillsTab'
import KafkaTab from '../components/settings/KafkaTab'
import LLMProvidersTab from '../components/settings/LLMProvidersTab'
import ModelAssignmentTab from '../components/settings/ModelAssignmentTab'

interface TabPanelProps {
  children?: React.ReactNode
  index: number
  value: number
}

function TabPanel({ children, value, index }: TabPanelProps) {
  return (
    <div role="tabpanel" hidden={value !== index}>
      {value === index && <Box sx={{ p: 2.5 }}>{children}</Box>}
    </div>
  )
}

// Map MCP server names to integration IDs where they don't match exactly
const SERVER_TO_INTEGRATION: Record<string, string> = {
  'aws-security': 'aws-security-hub',
  'gcp-scc': 'gcp-security',
}

const IS_DEV_MODE = import.meta.env.VITE_DEV_MODE === 'true'

// Define tabs — some are dev-only
const TAB_DEFS: { key: string; label: string; devOnly: boolean }[] = [
  { key: 'ai-config', label: 'AI Config', devOnly: false },
  { key: 's3', label: 'S3 Storage', devOnly: false },
  { key: 'integrations', label: 'Integrations / MCP', devOnly: false },
  { key: 'users', label: 'Users', devOnly: false },
  { key: 'agents', label: 'SOC Agents', devOnly: false },
  { key: 'autoinvestigate', label: 'Auto Investigate', devOnly: false },
  { key: 'skills', label: 'Skills', devOnly: false },
  { key: 'kafka', label: 'Kafka', devOnly: false },
  { key: 'general', label: 'General', devOnly: false },
  { key: 'dev', label: 'Developer', devOnly: true },
]

export default function Settings() {
  const theme = useTheme()
  const [searchParams] = useSearchParams()

  // Compute visible tabs and index lookup based on dev mode
  const visibleTabs = useMemo(() => TAB_DEFS.filter(t => !t.devOnly || IS_DEV_MODE), [])
  const tabIndex = useMemo(() => {
    const map: Record<string, number> = {}
    visibleTabs.forEach((t, i) => { map[t.key] = i })
    // Aliases
    map['mcp'] = map['integrations']
    map['github'] = map['integrations']
    map['postgresql'] = map['dev'] ?? map['integrations'] // postgresql now lives in dev tab
    map['detection-rules'] = map['integrations'] // detection rules now lives inside integrations tab
    map['claude'] = map['ai-config'] // legacy alias — "Claude" tab renamed to "AI Config" in #88/#89
    return map
  }, [visibleTabs])

  const getInitialTab = () => {
    const tabParam = searchParams.get('tab')
    if (tabParam && tabIndex[tabParam] !== undefined) {
      return tabIndex[tabParam]
    }
    return 0
  }

  const [currentTab, setCurrentTab] = useState(getInitialTab())
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const { notificationsEnabled, setNotificationsEnabled, permissionGranted } = useNotifications()

  const [claudeConfig, setClaudeConfig] = useState({ api_key: '', configured: false })
  const [s3Config, setS3Config] = useState({
    bucket_name: '', region: 'us-east-1', auth_method: 'credentials', aws_profile: '',
    access_key_id: '', secret_access_key: '', session_token: '',
    findings_path: 'findings.json', cases_path: 'cases.json', parquet_prefix: '', configured: false,
  })
  const [mcpServers, setMcpServers] = useState<string[]>([])
  const [mcpStatuses, setMcpStatuses] = useState<Record<string, string>>({})
  const [mcpEnabled, setMcpEnabled] = useState<Record<string, boolean>>({})
  const [postgresqlConfig, setPostgresqlConfig] = useState({ connection_string: '', configured: false })
  const [storageStatus, setStorageStatus] = useState<any>(null)
  const [storageHealth, setStorageHealth] = useState<any>(null)
  const [reconnecting, setReconnecting] = useState(false)
  const [integrationsConfig, setIntegrationsConfig] = useState({
    enabled_integrations: [] as string[],
    integrations: {} as Record<string, any>,
    configured: false,
  })
  const [wizardOpen, setWizardOpen] = useState(false)
  const [selectedIntegration, setSelectedIntegration] = useState<string | null>(null)
  const [customIntegrationBuilderOpen, setCustomIntegrationBuilderOpen] = useState(false)
  const [mcpSearch, setMcpSearch] = useState('')
  const [generalConfig, setGeneralConfig] = useState({
    auto_start_sync: false, show_notifications: true, theme: 'dark', enable_keyring: false,
  })
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean; title: string; message: string; onConfirm: () => void
  }>({ open: false, title: '', message: '', onConfirm: () => {} })
  const [s3HelpOpen, setS3HelpOpen] = useState(false)
  const [s3BrowsePrefix, setS3BrowsePrefix] = useState('')
  const [s3Files, setS3Files] = useState<{ key: string; size: number; last_modified: string }[]>([])
  const [s3FilesLoading, setS3FilesLoading] = useState(false)
  const [s3FilesLoaded, setS3FilesLoaded] = useState(false)
  const [s3SelectedKeys, setS3SelectedKeys] = useState<Set<string>>(new Set())
  const [s3Ingesting, setS3Ingesting] = useState(false)
  const [s3IngestProgress, setS3IngestProgress] = useState({ done: 0, total: 0 })
  const [s3IngestResults, setS3IngestResults] = useState<{ key: string; success: boolean; message: string }[]>([])
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState<{ success: boolean; message: string } | null>(null)
  const [clearingFindings, setClearingFindings] = useState(false)
  const [clearFindingsResult, setClearFindingsResult] = useState<{ success: boolean; message: string } | null>(null)
  const [confirmClearOpen, setConfirmClearOpen] = useState(false)
  const [detectionRulesOpen, setDetectionRulesOpen] = useState(false)
  const [splunkStatus, setSplunkStatus] = useState<any>(null)
  const [splunkLoading, setSplunkLoading] = useState(false)

  useEffect(() => { loadConfigs() }, [])
  useEffect(() => { setGeneralConfig(prev => ({ ...prev, show_notifications: notificationsEnabled })) }, [notificationsEnabled])
  useEffect(() => {
    if (currentTab === tabIndex['integrations']) { loadMcpServers() }
    else if (IS_DEV_MODE && currentTab === tabIndex['dev']) { loadSplunkStatus(); loadStorageStatus() }
  }, [currentTab, tabIndex])

  useEffect(() => {
    const tabParam = searchParams.get('tab')
    if (tabParam && tabIndex[tabParam] !== undefined) {
      setCurrentTab(tabIndex[tabParam])
    }
  }, [searchParams, tabIndex])

  // ---- Splunk handlers ----
  const loadSplunkStatus = async () => {
    try {
      const response = await localServicesApi.getSplunkStatus()
      setSplunkStatus(response.data)
    } catch (error) {
      console.error('Error loading Splunk status:', error)
    }
  }

  const handleStartSplunk = async () => {
    setSplunkLoading(true)
    try {
      const response = await localServicesApi.startSplunk()
      setMessage({ type: 'success', text: response.data.message || 'Splunk is starting' })
      setTimeout(() => loadSplunkStatus(), 2000)
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to start Splunk' })
    } finally { setSplunkLoading(false) }
  }

  const handleStopSplunk = async () => {
    setSplunkLoading(true)
    try {
      const response = await localServicesApi.stopSplunk()
      setMessage({ type: 'success', text: response.data.message || 'Splunk stopped' })
      setTimeout(() => loadSplunkStatus(), 1000)
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to stop Splunk' })
    } finally { setSplunkLoading(false) }
  }

  const handleRestartSplunk = async () => {
    setSplunkLoading(true)
    try {
      const response = await localServicesApi.restartSplunk()
      setMessage({ type: 'success', text: response.data.message || 'Splunk restarted' })
      setTimeout(() => loadSplunkStatus(), 2000)
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to restart Splunk' })
    } finally { setSplunkLoading(false) }
  }

  // ---- Config loaders ----
  const loadConfigs = async () => {
    try { await loadCustomIntegrations() } catch { /* ignore */ }
    if (IS_DEV_MODE) {
      try {
        const postgresqlData = await configApi.getPostgreSQL()
        setPostgresqlConfig({ ...postgresqlConfig, configured: postgresqlData.data?.configured || false })
      } catch { /* ignore */ }
    }
    try {
      const [claude, s3, integrations, general] = await Promise.all([
        configApi.getClaude().catch(() => ({ data: { configured: false } })),
        configApi.getS3().catch(() => ({ data: { configured: false } })),
        configApi.getIntegrations().catch(() => ({ data: { configured: false, enabled_integrations: [], integrations: {} } })),
        configApi.getGeneral().catch(() => ({ data: { auto_start_sync: false, show_notifications: true, theme: 'dark', enable_keyring: false } })),
      ])
      setClaudeConfig(prev => ({ ...prev, configured: claude.data.configured }))
      setS3Config(prev => ({ ...prev, ...s3.data }))
      if (s3.data.parquet_prefix) setS3BrowsePrefix(s3.data.parquet_prefix)
      setIntegrationsConfig(prev => ({ ...prev, ...integrations.data }))
      setGeneralConfig(prev => ({ ...prev, ...general.data }))
    } catch { /* ignore */ }
  }

  const loadMcpServers = async () => {
    try {
      const [servers, statuses] = await Promise.all([mcpApi.listServers(), mcpApi.getStatuses()])
      setMcpServers(servers.data.servers || [])
      const statusList = statuses.data.statuses || []
      const statusDict: Record<string, string> = {}
      const enabledDict: Record<string, boolean> = {}
      if (Array.isArray(statusList)) {
        statusList.forEach((item: any) => {
          if (item.name && item.status) statusDict[item.name] = item.status
          if (item.name) enabledDict[item.name] = !!item.enabled
        })
      }
      setMcpStatuses(statusDict)
      setMcpEnabled(enabledDict)
    } catch { /* ignore */ }
  }

  const loadStorageStatus = async () => {
    try {
      const [status, health] = await Promise.all([storageApi.getStatus(), storageApi.getHealth()])
      setStorageStatus(status.data)
      setStorageHealth(health.data)
    } catch { /* ignore */ }
  }

  // ---- Action handlers ----
  const handleReconnectDatabase = async () => {
    setReconnecting(true)
    try {
      const response = await storageApi.reconnect()
      if (response.data.success) {
        setMessage({ type: 'success', text: 'Reconnected to PostgreSQL' })
        await loadStorageStatus()
      } else {
        setMessage({ type: 'error', text: response.data.message || 'Failed to reconnect' })
      }
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.message || 'Failed to reconnect' })
    } finally {
      setReconnecting(false)
      setTimeout(() => setMessage(null), 5000)
    }
  }

  const showConfirm = (title: string, msg: string, onConfirm: () => void) => {
    setConfirmDialog({ open: true, title, message: msg, onConfirm })
  }
  const handleConfirmClose = () => { setConfirmDialog({ ...confirmDialog, open: false }) }
  const handleConfirmAction = async () => { handleConfirmClose(); await confirmDialog.onConfirm() }

  const doSaveClaude = async () => {
    try { await configApi.setClaude(claudeConfig.api_key); setMessage({ type: 'success', text: 'Claude API key saved' }); await loadConfigs() }
    catch { setMessage({ type: 'error', text: 'Failed to save' }) }
    setTimeout(() => setMessage(null), 3000)
  }
  const handleSaveClaude = () => {
    if (!claudeConfig.api_key.trim()) { setMessage({ type: 'error', text: 'API key cannot be empty' }); setTimeout(() => setMessage(null), 3000); return }
    showConfirm('Save Claude API Key', 'Are you sure you want to save this API key? This will overwrite any existing key.', doSaveClaude)
  }

  const doSaveS3 = async () => {
    try {
      await configApi.setS3(s3Config)
      setMessage({ type: 'success', text: 'S3 configuration saved' })
      await loadConfigs()
    } catch { setMessage({ type: 'error', text: 'Failed to save' }) }
    setTimeout(() => setMessage(null), 3000)
  }
  const handleSaveS3 = () => {
    if (!s3Config.bucket_name.trim()) { setMessage({ type: 'error', text: 'Bucket name is required' }); setTimeout(() => setMessage(null), 3000); return }
    showConfirm('Save S3 Configuration', 'Are you sure you want to save these S3 settings?', doSaveS3)
  }

  const handleBrowseS3 = async () => {
    setS3FilesLoading(true)
    setS3IngestResults([])
    try {
      const response = await ingestionApi.listS3Files(s3BrowsePrefix)
      setS3Files(response.data.files || [])
      setS3FilesLoaded(true)
      setS3SelectedKeys(new Set())
    } catch (error: any) {
      setMessage({ type: 'error', text: error.response?.data?.detail || 'Failed to list S3 files' })
    } finally {
      setS3FilesLoading(false)
    }
  }

  const handleToggleS3File = (key: string) => {
    setS3SelectedKeys(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const handleToggleAllS3Files = () => {
    if (s3SelectedKeys.size === s3Files.length) {
      setS3SelectedKeys(new Set())
    } else {
      setS3SelectedKeys(new Set(s3Files.map(f => f.key)))
    }
  }

  const handleIngestSelected = async () => {
    const keys = Array.from(s3SelectedKeys)
    if (keys.length === 0) return
    setS3Ingesting(true)
    setS3IngestProgress({ done: 0, total: keys.length })
    const results: { key: string; success: boolean; message: string }[] = []

    for (const key of keys) {
      try {
        const response = await ingestionApi.ingestS3File(key)
        results.push({ key, success: response.data.success, message: response.data.message })
      } catch (error: any) {
        results.push({ key, success: false, message: error.response?.data?.detail || 'Ingestion failed' })
      }
      setS3IngestProgress(prev => ({ ...prev, done: prev.done + 1 }))
    }

    setS3IngestResults(results)
    setS3Ingesting(false)
    setS3SelectedKeys(new Set())

    const successCount = results.filter(r => r.success).length
    if (successCount === keys.length) {
      setMessage({ type: 'success', text: `Successfully ingested ${successCount} file(s)` })
    } else {
      setMessage({ type: 'error', text: `Ingested ${successCount}/${keys.length} file(s). Check results below.` })
    }
    setTimeout(() => setMessage(null), 5000)
  }

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
  }

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] || null
    setUploadFile(file)
    setUploadResult(null)
  }

  const handleUploadFile = async () => {
    if (!uploadFile) return
    setUploading(true)
    setUploadResult(null)
    try {
      const response = await ingestionApi.uploadFile(uploadFile)
      setUploadResult({ success: response.data.success, message: response.data.message })
      if (response.data.success) {
        setMessage({ type: 'success', text: response.data.message })
      } else {
        setMessage({ type: 'error', text: response.data.message || 'Upload completed with issues' })
      }
    } catch (error: any) {
      const msg = error.response?.data?.detail || 'Upload failed'
      setUploadResult({ success: false, message: msg })
      setMessage({ type: 'error', text: msg })
    } finally {
      setUploading(false)
      setTimeout(() => setMessage(null), 5000)
    }
  }

  const handleClearFindings = async () => {
    setClearingFindings(true)
    setClearFindingsResult(null)
    try {
      const response = await findingsApi.deleteAll()
      const data = response.data as any
      setClearFindingsResult({ success: true, message: data.message || `Deleted ${data.deleted} findings` })
      setMessage({ type: 'success', text: data.message || 'Findings cleared' })
    } catch (error: any) {
      const msg = error.response?.data?.detail || 'Failed to clear findings'
      setClearFindingsResult({ success: false, message: msg })
      setMessage({ type: 'error', text: msg })
    } finally {
      setClearingFindings(false)
      setConfirmClearOpen(false)
      setTimeout(() => setMessage(null), 5000)
    }
  }

  const doSaveGeneral = async () => {
    try { await configApi.setGeneral(generalConfig); setMessage({ type: 'success', text: 'Settings saved' }) }
    catch { setMessage({ type: 'error', text: 'Failed to save' }) }
    setTimeout(() => setMessage(null), 3000)
  }
  const handleSaveGeneral = () => { showConfirm('Save Settings', 'Are you sure you want to save these general settings?', doSaveGeneral) }

  const handleNotificationToggle = async (checked: boolean) => {
    try {
      if (checked && !notificationService.isSupported()) { setMessage({ type: 'error', text: 'Browser does not support notifications' }); return }
      await setNotificationsEnabled(checked)
      setGeneralConfig({ ...generalConfig, show_notifications: checked })
      if (checked && permissionGranted) notificationService.notifyGeneric('Notifications Enabled', 'You will receive desktop notifications', { severity: 'success' })
    } catch { setMessage({ type: 'error', text: 'Failed to enable notifications' }) }
  }

  const doSavePostgreSQL = async () => {
    try { await configApi.setPostgreSQL(postgresqlConfig.connection_string); setMessage({ type: 'success', text: 'PostgreSQL config saved' }); await loadConfigs() }
    catch { setMessage({ type: 'error', text: 'Failed to save' }) }
    setTimeout(() => setMessage(null), 3000)
  }
  const handleSavePostgreSQL = () => {
    if (!postgresqlConfig.connection_string.trim()) { setMessage({ type: 'error', text: 'Connection string cannot be empty' }); setTimeout(() => setMessage(null), 3000); return }
    showConfirm('Save PostgreSQL Configuration', 'Are you sure you want to save this connection string?', doSavePostgreSQL)
  }

  const handleToggleServer = async (serverName: string, enabled: boolean) => {
    try {
      await mcpApi.setServerEnabled(serverName, enabled)
      setMcpEnabled(prev => ({ ...prev, [serverName]: enabled }))
      setMessage({ type: 'success', text: `${serverName} ${enabled ? 'enabled' : 'disabled'}` })
      loadMcpServers()
    } catch { setMessage({ type: 'error', text: `Failed to ${enabled ? 'enable' : 'disable'} ${serverName}` }) }
    setTimeout(() => setMessage(null), 3000)
  }

  const handleSaveIntegration = async (integrationId: string, config: Record<string, any>) => {
    try {
      const updatedIntegrations = { ...integrationsConfig.integrations, [integrationId]: config }
      const updatedEnabled = [...integrationsConfig.enabled_integrations]
      if (!updatedEnabled.includes(integrationId)) updatedEnabled.push(integrationId)
      await configApi.setIntegrations({ enabled_integrations: updatedEnabled, integrations: updatedIntegrations })
      setIntegrationsConfig({ ...integrationsConfig, enabled_integrations: updatedEnabled, integrations: updatedIntegrations, configured: true })
      setMessage({ type: 'success', text: `${integrationId} configured` })
    } catch { throw new Error('Failed to save') }
    setTimeout(() => setMessage(null), 3000)
  }

  // ---- Helpers ----
  const getIntegrationForServer = (serverName: string) => {
    const integrationId = SERVER_TO_INTEGRATION[serverName] || serverName
    return getAllIntegrations().find(i => i.id === integrationId)
  }

  // ---- Servers still using custom tools/* implementations (MCP replacement pending or unavailable) ----
  const WIP_SERVERS = new Set([
    // No MCP replacement available
    'carbon-black', 'hybrid-analysis', 'anyrun',
    // Vendor MCP exists but scope differs — keeping custom for now
    'alienvault-otx', 'palo-alto',
    // MCP replacement identified but not yet integrated
    'slack', 'misp', 'ip-geolocation', 'url-analysis',
    'microsoft-defender', 'azure-ad', 'microsoft-teams',
  ])

  // ---- Integration category → MCP display category mapping ----
  const INTEGRATION_CAT_TO_MCP_CAT: Record<string, string> = {
    'Reference / Platform': 'Reference Servers',
    'Threat Intelligence': 'Threat Intelligence',
    'EDR/XDR': 'EDR / XDR',
    'SIEM': 'SIEM / Data Lake',
    'Data Pipeline': 'SIEM / Data Lake',
    'Cloud Security': 'Cloud Security',
    'Identity & Access': 'Identity & Access',
    'Network Security': 'Network Security',
    'Incident Management': 'Incident Management',
    'Communications': 'Communications',
    'Sandbox Analysis': 'Sandbox / Analysis',
    'Forensics & Analysis': 'Forensics & Analysis',
  }

  const REVERSE_INTEGRATION_MAP = useMemo(() => {
    const map: Record<string, string> = {}
    Object.entries(SERVER_TO_INTEGRATION).forEach(([server, integrationId]) => {
      map[integrationId] = server
    })
    return map
  }, [])

  const implementedIds = useMemo(() => {
    const ids = new Set<string>()
    mcpServers.forEach(name => {
      ids.add(name)
      const mappedId = SERVER_TO_INTEGRATION[name]
      if (mappedId) ids.add(mappedId)
    })
    return ids
  }, [mcpServers])

  const unimplementedByMcpCat = useMemo(() => {
    const map: Record<string, IntegrationMetadata[]> = {}
    getAllIntegrations().forEach(i => {
      if (implementedIds.has(i.id)) return
      if (REVERSE_INTEGRATION_MAP[i.id]) return
      const cat = INTEGRATION_CAT_TO_MCP_CAT[i.category || ''] || 'Other'
      if (!map[cat]) map[cat] = []
      map[cat].push(i)
    })
    return map
  }, [implementedIds, REVERSE_INTEGRATION_MAP])

  const totalUnimplemented = useMemo(
    () => Object.values(unimplementedByMcpCat).reduce((sum, arr) => sum + arr.length, 0),
    [unimplementedByMcpCat],
  )

  // ---- MCP server descriptions ----
  const SERVER_DESCRIPTIONS: Record<string, string> = {
    // Internal / Platform
    'deeptempo-findings': 'Core findings and case management. Required for the investigation workflow, case creation, and findings display.',
    'tempo-flow': 'Orchestrates multi-step agent workflows and playbook execution. Required for automated investigation chains.',
    'approval': 'Human-in-the-loop approval queue for response actions (isolate host, block IP, etc.). Prevents the AI from taking destructive actions without analyst review.',
    'attack-layer': 'Maps findings to MITRE ATT&CK techniques and generates Navigator layers for coverage visualization.',
    'security-detections': 'Searches across 30,000+ detection rules (Sigma, Splunk, Elastic, KQL). Powers detection gap analysis and rule recommendations.',
    // Reference
    'github': 'Access GitHub repos, issues, PRs, and code search. Useful for looking up detection rule history, IaC configs, or creating remediation issues.',
    // EDR / XDR
    'crowdstrike': 'Query CrowdStrike Falcon for endpoint detections, host info, and IOC management. Requires Falcon API credentials.',
    'sentinelone': 'Query SentinelOne for endpoint threats, agent status, and threat remediation via the Purple AI MCP.',
    'carbon-black': 'Query VMware Carbon Black for endpoint events, process trees, and binary analysis.',
    'microsoft-defender': 'Query Microsoft Defender for Endpoint alerts, device info, and advanced hunting. May overlap with Sentinel.',
    // SIEM / Data Lake
    'splunk': 'Run SPL searches against Splunk for log analysis, correlation searches, and alert triage.',
    'azure-sentinel': 'Query Microsoft Sentinel via KQL for security logs, incidents, and custom detection rules.',
    'gcp-secops': 'Query Google SecOps (Chronicle) for UDM security events, detection rules, and threat investigation.',
    'cribl-stream': 'Manage Cribl Stream data pipelines — inspect routes, check data flow, and troubleshoot ingestion.',
    // Threat Intelligence
    'virustotal': 'Look up file hashes, URLs, domains, and IPs against VirusTotal for malware and reputation data.',
    'gcp-threat-intel': 'Google Threat Intelligence (Mandiant + VirusTotal) for threat actor profiles, campaigns, and IOC enrichment.',
    'shodan': 'Search Shodan for internet-exposed devices, open ports, and service banners on IPs and domains.',
    'alienvault-otx': 'Query AlienVault OTX for community-sourced threat intelligence pulses, IOCs, and threat reports.',
    'misp': 'Connect to a MISP instance for threat sharing, IOC lookups, and collaborative threat intelligence.',
    // Cloud Security
    'aws-security': 'AWS Security assessment covering GuardDuty, Security Hub, Inspector, and IAM Access Analyzer findings.',
    'gcp-scc': 'Google Cloud Security Command Center for cloud asset inventory, vulnerability findings, and threat detection.',
    'palo-alto': 'Query Palo Alto Networks firewalls for threat logs, traffic analysis, and IP/domain blocking.',
    // Identity & Access
    'okta': 'Query Okta for user authentication events, suspicious sign-ins, and identity-based threat investigation.',
    'azure-ad': 'Query Microsoft Entra ID (Azure AD) for sign-in logs, risky users, and directory lookups.',
    // Incident Management
    'jira': 'Create and manage Jira issues for incident tracking, remediation tasks, and SOC workflow integration.',
    'pagerduty': 'Trigger and manage PagerDuty incidents for on-call alerting and escalation during security events.',
    'slack': 'Send alerts and investigation summaries to Slack channels. Enables team collaboration during incidents.',
    'microsoft-teams': 'Post alerts and case updates to Microsoft Teams channels for SOC team communication.',
    // Sandbox / Analysis
    'joe-sandbox': 'Submit files and URLs to Joe Sandbox for deep malware analysis with behavioral reports.',
    'hybrid-analysis': 'Submit samples to CrowdStrike Hybrid Analysis for free automated malware analysis and IOC extraction.',
    'anyrun': 'Interactive malware sandbox for real-time analysis with process monitoring and network capture.',
    'url-analysis': 'Analyze suspicious URLs for phishing indicators, redirects, and malicious content.',
    'ip-geolocation': 'Look up geographic location, ISP, and organization info for IP addresses during investigations.',
  }

  // ---- MCP server card renderer ----
  const CARD_HEIGHT = 172
  const renderServerCard = (serverName: string) => {
    const isEnabled = mcpEnabled[serverName] || false
    const status = mcpStatuses[serverName] || 'stopped'
    const isRunning = status === 'running'
    const integration = getIntegrationForServer(serverName)
    const isConfigured = integration ? integrationsConfig.enabled_integrations.includes(integration.id) : false

    // Pretty display name: "aws-security" → "AWS Security", etc.
    const displayName = serverName
      .split('-')
      .map(w => w.charAt(0).toUpperCase() + w.slice(1))
      .join(' ')

    // Indicator: green = on, red = off, gray = needs configuration
    const needsConfig = integration && !isConfigured
    const indicatorColor = isEnabled ? '#4caf50' : needsConfig ? '#9e9e9e' : '#f44336'
    const indicatorLabel = isEnabled ? (isRunning ? 'Running' : 'Enabled') : needsConfig ? 'Not Configured' : 'Off'

    // Shared button style
    const btnSx = {
      textTransform: 'none' as const,
      fontSize: '0.7rem',
      py: 0.4,
      px: 1.25,
      minWidth: 0,
      borderColor: 'divider',
      color: 'text.secondary',
      '&:hover': { borderColor: 'primary.main', color: 'primary.main', bgcolor: alpha(theme.palette.primary.main, 0.06) },
    }

    return (
      <Grid item xs={12} sm={6} md={4} lg={3} key={serverName}>
        <Card
          variant="outlined"
          sx={{
            height: CARD_HEIGHT,
            display: 'flex',
            flexDirection: 'column',
            borderColor: isEnabled ? alpha(theme.palette.primary.main, 0.5) : 'divider',
            borderWidth: 1,
            bgcolor: isEnabled ? alpha(theme.palette.primary.main, 0.03) : 'background.paper',
            transition: 'border-color 0.2s, background-color 0.2s',
            '&:hover': { borderColor: isEnabled ? 'primary.main' : alpha(theme.palette.text.primary, 0.25) },
          }}
        >
          <CardContent sx={{ display: 'flex', flexDirection: 'column', flex: 1, p: 2, '&:last-child': { pb: 1.5 } }}>
            {/* Row 1: Name + WIP badge + Toggle */}
            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 0.75 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
                <Typography variant="body2" noWrap sx={{ fontWeight: 600, lineHeight: 1.3 }}>
                  {displayName}
                </Typography>
                {WIP_SERVERS.has(serverName) && (
                  <Chip label="WIP" size="small" sx={{ height: 18, fontSize: '0.6rem', fontWeight: 700, bgcolor: alpha('#ff9800', 0.15), color: '#ff9800', borderColor: alpha('#ff9800', 0.4), borderWidth: 1, borderStyle: 'solid' }} />
                )}
              </Box>
              <Switch checked={isEnabled} onChange={(e) => handleToggleServer(serverName, e.target.checked)} size="small" />
            </Box>

            {/* Row 2: Description */}
            {SERVER_DESCRIPTIONS[serverName] && (
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{
                  display: '-webkit-box',
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                  lineHeight: 1.35,
                  fontSize: '0.68rem',
                  mb: 0.75,
                }}
              >
                {SERVER_DESCRIPTIONS[serverName]}
              </Typography>
            )}

            {/* Row 3: Status indicator — always clickable to open config for integrations */}
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 'auto' }}>
              <Chip
                label={integration && needsConfig && !isEnabled ? 'Configure' : indicatorLabel}
                size="small"
                onClick={integration ? () => { setSelectedIntegration(integration.id); setWizardOpen(true) } : undefined}
                sx={{
                  height: 22,
                  fontSize: '0.68rem',
                  fontWeight: 600,
                  cursor: integration ? 'pointer' : 'default',
                  borderWidth: 1.5,
                  borderStyle: 'solid',
                  borderColor: alpha(indicatorColor, 0.5),
                  bgcolor: alpha(indicatorColor, 0.1),
                  color: indicatorColor,
                  '& .MuiChip-icon': { color: indicatorColor },
                  ...(integration && {
                    '&:hover': { bgcolor: alpha(indicatorColor, 0.2), borderColor: indicatorColor },
                  }),
                }}
                icon={<Box component="span" sx={{ width: 7, height: 7, borderRadius: '50%', bgcolor: indicatorColor, ml: '5px !important', boxShadow: `0 0 4px ${alpha(indicatorColor, 0.6)}` }} />}
              />
            </Box>

            {/* Row 4: Secondary actions — pinned to bottom */}
            <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap', pt: 0.75 }}>
              {integration?.docs_url && (
                <Button
                  size="small"
                  variant="outlined"
                  endIcon={<OpenInNewIcon sx={{ fontSize: '11px !important' }} />}
                  href={integration.docs_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  sx={btnSx}
                >
                  Docs
                </Button>
              )}
              {serverName === 'security-detections' && (
                <Button size="small" variant="outlined" onClick={() => setDetectionRulesOpen(true)} sx={btnSx}>
                  Manage Rules
                </Button>
              )}
            </Box>
          </CardContent>
        </Card>
      </Grid>
    )
  }

  // ---- Unimplemented integration card renderer (WIP) ----
  const renderUnimplementedCard = (integration: IntegrationMetadata) => {
    const wipColor = '#ff9800'
    const grayColor = '#9e9e9e'

    const btnSx = {
      textTransform: 'none' as const,
      fontSize: '0.7rem',
      py: 0.4,
      px: 1.25,
      minWidth: 0,
      borderColor: 'divider',
      color: 'text.secondary',
      '&:hover': { borderColor: 'primary.main', color: 'primary.main', bgcolor: alpha(theme.palette.primary.main, 0.06) },
    }

    return (
      <Grid item xs={12} sm={6} md={4} lg={3} key={integration.id}>
        <Card
          variant="outlined"
          sx={{
            height: CARD_HEIGHT,
            display: 'flex',
            flexDirection: 'column',
            borderColor: alpha(grayColor, 0.3),
            borderWidth: 1,
            bgcolor: 'background.paper',
            opacity: 0.75,
            transition: 'border-color 0.2s, opacity 0.2s',
            '&:hover': { borderColor: alpha(grayColor, 0.6), opacity: 0.9 },
          }}
        >
          <CardContent sx={{ display: 'flex', flexDirection: 'column', flex: 1, p: 2, '&:last-child': { pb: 1.5 } }}>
            <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 0.75 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
                <Typography variant="body2" noWrap sx={{ fontWeight: 600, lineHeight: 1.3 }}>
                  {integration.name}
                </Typography>
                <Chip label="WIP" size="small" sx={{ height: 18, fontSize: '0.6rem', fontWeight: 700, bgcolor: alpha(wipColor, 0.15), color: wipColor, borderColor: alpha(wipColor, 0.4), borderWidth: 1, borderStyle: 'solid' }} />
              </Box>
            </Box>

            {integration.description && (
              <Typography
                variant="caption"
                color="text.secondary"
                sx={{
                  display: '-webkit-box',
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                  lineHeight: 1.35,
                  fontSize: '0.68rem',
                  mb: 0.75,
                }}
              >
                {integration.description}
              </Typography>
            )}

            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 'auto' }}>
              <Chip
                label="Planned"
                size="small"
                sx={{
                  height: 22,
                  fontSize: '0.68rem',
                  fontWeight: 600,
                  borderWidth: 1.5,
                  borderStyle: 'solid',
                  borderColor: alpha(grayColor, 0.5),
                  bgcolor: alpha(grayColor, 0.1),
                  color: grayColor,
                }}
                icon={<Box component="span" sx={{ width: 7, height: 7, borderRadius: '50%', bgcolor: grayColor, ml: '5px !important' }} />}
              />
            </Box>

            <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap', pt: 0.75 }}>
              {integration.docs_url && (
                <Button
                  size="small"
                  variant="outlined"
                  endIcon={<OpenInNewIcon sx={{ fontSize: '11px !important' }} />}
                  href={integration.docs_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  sx={btnSx}
                >
                  Docs
                </Button>
              )}
            </Box>
          </CardContent>
        </Card>
      </Grid>
    )
  }

  const MCP_CATEGORIES = [
    { label: 'Internal / Platform', filter: (n: string) => ['deeptempo-findings', 'tempo-flow', 'approval', 'attack-layer', 'security-detections'].includes(n) },
    { label: 'Reference Servers', filter: (n: string) => ['github'].includes(n) },
    { label: 'EDR / XDR', filter: (n: string) => ['crowdstrike', 'sentinelone', 'carbon-black', 'microsoft-defender'].includes(n) },
    { label: 'SIEM / Data Lake', filter: (n: string) => ['splunk', 'azure-sentinel', 'gcp-secops', 'cribl-stream'].includes(n) },
    { label: 'Threat Intelligence', filter: (n: string) => ['virustotal', 'gcp-threat-intel', 'shodan', 'alienvault-otx', 'misp'].includes(n) },
    { label: 'Cloud Security', filter: (n: string) => ['aws-security', 'gcp-scc', 'palo-alto'].includes(n) },
    { label: 'Identity & Access', filter: (n: string) => ['okta', 'azure-ad'].includes(n) },
    { label: 'Network Security', filter: (_n: string) => false },
    { label: 'Incident Management', filter: (n: string) => ['jira', 'pagerduty', 'slack', 'microsoft-teams'].includes(n) },
    { label: 'Communications', filter: (_n: string) => false },
    { label: 'Sandbox / Analysis', filter: (n: string) => ['joe-sandbox', 'hybrid-analysis', 'anyrun', 'url-analysis', 'ip-geolocation'].includes(n) },
    { label: 'Forensics & Analysis', filter: (_n: string) => false },
  ]

  const CATEGORIZED_NAMES = new Set(MCP_CATEGORIES.flatMap(c => mcpServers.filter(c.filter)))

  // ---- Tab content renderers ----
  const renderTabContent = (tabKey: string, idx: number) => {
    switch (tabKey) {
      case 'ai-config':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <Box sx={{ maxWidth: 900 }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>Default Anthropic API Key</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                Used by the built-in Anthropic provider (<code>anthropic-default</code>). For Ollama / OpenAI / custom Anthropic accounts, add a provider below.
              </Typography>
              <Box sx={{ mb: 2 }}>
                <Chip label={claudeConfig.configured ? 'API key configured' : 'API key not configured'} color={claudeConfig.configured ? 'success' : 'default'} size="small" icon={claudeConfig.configured ? <CheckIcon /> : undefined} />
              </Box>
              <TextField fullWidth label="API Key" type="password" value={claudeConfig.api_key} onChange={(e) => setClaudeConfig({ ...claudeConfig, api_key: e.target.value })} sx={{ mb: 2 }} />
              <Button variant="contained" startIcon={<SaveIcon />} onClick={handleSaveClaude}>Save</Button>
            </Box>
            <Box sx={{ mt: 4, maxWidth: 1100 }}>
              <ModelAssignmentTab setMessage={setMessage} />
            </Box>
            <Box sx={{ mt: 4 }}>
              <LLMProvidersTab setMessage={setMessage} />
            </Box>
          </TabPanel>
        )

      case 's3':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <Box sx={{ maxWidth: 600 }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>S3 Storage</Typography>
              <Box sx={{ mb: 2 }}>
                <Chip label={(s3Config.configured && s3Config.bucket_name) ? 'S3 configured' : 'S3 not configured'} color={(s3Config.configured && s3Config.bucket_name) ? 'success' : 'default'} size="small" icon={(s3Config.configured && s3Config.bucket_name) ? <CheckIcon /> : undefined} />
              </Box>
              <TextField fullWidth label="Bucket Name" placeholder="my-bucket or s3://my-bucket/prefix/" value={s3Config.bucket_name} onChange={(e) => {
                const val = e.target.value
                if (val.startsWith('s3://')) {
                  const stripped = val.slice(5)
                  const slashIdx = stripped.indexOf('/')
                  if (slashIdx > 0) {
                    const bucket = stripped.slice(0, slashIdx)
                    let path = stripped.slice(slashIdx + 1).replace(/\/*$/, '')
                    const lastSeg = path.includes('/') ? path.split('/').pop()! : path
                    if (lastSeg.includes('.')) {
                      path = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''
                    }
                    setS3Config({ ...s3Config, bucket_name: bucket, parquet_prefix: path ? path + '/' : '' })
                    return
                  }
                }
                setS3Config({ ...s3Config, bucket_name: val })
              }} sx={{ mb: 2 }} />
              <TextField fullWidth label="Region" value={s3Config.region} onChange={(e) => setS3Config({ ...s3Config, region: e.target.value })} sx={{ mb: 2 }} />

              <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>Authentication Method</Typography>
              <ToggleButtonGroup
                value={s3Config.auth_method}
                exclusive
                onChange={(_e, val) => { if (val) setS3Config({ ...s3Config, auth_method: val }) }}
                size="small"
                sx={{ mb: 2 }}
              >
                <ToggleButton value="credentials">Manual Credentials</ToggleButton>
                <ToggleButton value="profile">AWS Profile (SSO)</ToggleButton>
              </ToggleButtonGroup>

              {s3Config.auth_method === 'profile' ? (
                <>
                  <TextField fullWidth label="AWS Profile Name" value={s3Config.aws_profile} onChange={(e) => setS3Config({ ...s3Config, aws_profile: e.target.value })} placeholder="e.g. my-sso-profile" helperText={<>Name of the profile in ~/.aws/config. Run <code>aws sso login --profile &lt;name&gt;</code> before using.</>} sx={{ mb: 2 }} />
                </>
              ) : (
                <>
                  <TextField fullWidth label="Access Key ID" value={s3Config.access_key_id} onChange={(e) => setS3Config({ ...s3Config, access_key_id: e.target.value })} placeholder={s3Config.configured ? '(saved — leave blank to keep)' : ''} sx={{ mb: 2 }} />
                  <TextField fullWidth label="Secret Access Key" type="password" value={s3Config.secret_access_key} onChange={(e) => setS3Config({ ...s3Config, secret_access_key: e.target.value })} placeholder={s3Config.configured ? '(saved — leave blank to keep)' : ''} sx={{ mb: 2 }} />
                  <TextField fullWidth label="Session Token (optional)" type="password" value={s3Config.session_token} onChange={(e) => setS3Config({ ...s3Config, session_token: e.target.value })} placeholder={s3Config.configured ? '(saved — leave blank to keep)' : ''} helperText="Required for temporary AWS STS credentials (keys starting with ASIA)." sx={{ mb: 2 }} />
                </>
              )}

              <TextField fullWidth label="Default Path / Prefix" placeholder="e.g. lake/v1/embeddings/" value={s3Config.parquet_prefix} onChange={(e) => setS3Config({ ...s3Config, parquet_prefix: e.target.value })} helperText="S3 key prefix used as the default when browsing files. Tip: paste a full s3:// URI into Bucket Name to auto-populate." sx={{ mb: 2 }} />
              <Box sx={{ display: 'flex', gap: 1 }}>
                <Button variant="contained" startIcon={<SaveIcon />} onClick={handleSaveS3}>Save</Button>
                <Button variant="outlined" startIcon={<HelpIcon />} onClick={() => setS3HelpOpen(true)}>How to Configure Your S3 Bucket</Button>
              </Box>
            </Box>

            <Box sx={{ mt: 4, maxWidth: 900 }}>
              <Divider sx={{ mb: 3 }} />
              <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>Data Management</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                Clear all findings from the database to start fresh.
              </Typography>
              <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', mb: 1 }}>
                <Button
                  variant="outlined"
                  color="error"
                  startIcon={clearingFindings ? <CircularProgress size={16} color="inherit" /> : <DeleteSweepIcon />}
                  onClick={() => setConfirmClearOpen(true)}
                  disabled={clearingFindings}
                >
                  {clearingFindings ? 'Clearing...' : 'Clear All Findings'}
                </Button>
              </Box>
              {clearFindingsResult && (
                <Alert severity={clearFindingsResult.success ? 'success' : 'error'} sx={{ mt: 1, '& .MuiAlert-message': { fontSize: '0.85rem' } }}>
                  {clearFindingsResult.message}
                </Alert>
              )}
            </Box>

            <Dialog open={confirmClearOpen} onClose={() => setConfirmClearOpen(false)}>
              <DialogTitle>Clear All Findings?</DialogTitle>
              <DialogContent>
                <DialogContentText>
                  This will permanently delete all findings from the database. This action cannot be undone.
                </DialogContentText>
              </DialogContent>
              <DialogActions>
                <Button onClick={() => setConfirmClearOpen(false)}>Cancel</Button>
                <Button onClick={handleClearFindings} color="error" variant="contained" disabled={clearingFindings}>
                  {clearingFindings ? 'Clearing...' : 'Yes, Clear All'}
                </Button>
              </DialogActions>
            </Dialog>

            <Box sx={{ mt: 4, maxWidth: 900 }}>
              <Divider sx={{ mb: 3 }} />
              <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>Upload Local File</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                Upload a CSV, Parquet, JSON, or JSONL file from your computer to ingest directly.
              </Typography>

              <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center', mb: 1 }}>
                <Button
                  variant="outlined"
                  component="label"
                  startIcon={<UploadFileIcon />}
                  sx={{ textTransform: 'none' }}
                >
                  {uploadFile ? uploadFile.name : 'Choose File'}
                  <input
                    type="file"
                    hidden
                    accept=".csv,.parquet,.json,.jsonl,.ndjson"
                    onChange={handleFileSelect}
                  />
                </Button>
                {uploadFile && (
                  <Typography variant="body2" color="text.secondary">
                    {formatFileSize(uploadFile.size)}
                  </Typography>
                )}
                <Button
                  variant="contained"
                  startIcon={uploading ? <CircularProgress size={16} color="inherit" /> : <CloudDownloadIcon />}
                  onClick={handleUploadFile}
                  disabled={!uploadFile || uploading}
                >
                  {uploading ? 'Uploading...' : 'Ingest'}
                </Button>
              </Box>

              {uploadResult && (
                <Alert severity={uploadResult.success ? 'success' : 'error'} sx={{ mt: 1, '& .MuiAlert-message': { fontSize: '0.85rem' } }}>
                  {uploadResult.message}
                </Alert>
              )}
            </Box>

            {s3Config.configured && s3Config.bucket_name && (
              <Box sx={{ mt: 4, maxWidth: 900 }}>
                <Divider sx={{ mb: 3 }} />
                <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 0.5 }}>Browse & Ingest S3 Files</Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                  Browse your S3 bucket and select files to ingest manually.
                </Typography>

                <Box sx={{ display: 'flex', gap: 1, mb: 2, alignItems: 'center' }}>
                  <TextField
                    size="small"
                    label="Prefix / Folder"
                    placeholder="e.g. data/embeddings/"
                    value={s3BrowsePrefix}
                    onChange={(e) => setS3BrowsePrefix(e.target.value)}
                    sx={{ flex: 1, maxWidth: 400 }}
                  />
                  <Button
                    variant="contained"
                    startIcon={s3FilesLoading ? <CircularProgress size={16} color="inherit" /> : <FolderOpenIcon />}
                    onClick={handleBrowseS3}
                    disabled={s3FilesLoading}
                  >
                    {s3FilesLoading ? 'Loading...' : 'Browse'}
                  </Button>
                </Box>

                {s3FilesLoaded && s3Files.length === 0 && (
                  <Alert severity="info" sx={{ mb: 2 }}>No files found{s3BrowsePrefix ? ` under prefix "${s3BrowsePrefix}"` : ' in bucket'}.</Alert>
                )}

                {s3Files.length > 0 && (
                  <>
                    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 1 }}>
                      <Typography variant="body2" color="text.secondary">
                        {s3Files.length} file(s) found &middot; {s3SelectedKeys.size} selected
                      </Typography>
                      <Button
                        variant="contained"
                        size="small"
                        startIcon={s3Ingesting ? <CircularProgress size={14} color="inherit" /> : <CloudDownloadIcon />}
                        onClick={handleIngestSelected}
                        disabled={s3SelectedKeys.size === 0 || s3Ingesting}
                      >
                        {s3Ingesting
                          ? `Ingesting ${s3IngestProgress.done}/${s3IngestProgress.total}...`
                          : `Ingest Selected (${s3SelectedKeys.size})`}
                      </Button>
                    </Box>

                    {s3Ingesting && (
                      <LinearProgress
                        variant="determinate"
                        value={s3IngestProgress.total > 0 ? (s3IngestProgress.done / s3IngestProgress.total) * 100 : 0}
                        sx={{ mb: 1, borderRadius: 1 }}
                      />
                    )}

                    <TableContainer sx={{ border: 1, borderColor: 'divider', borderRadius: 2, maxHeight: 420 }}>
                      <Table size="small" stickyHeader>
                        <TableHead>
                          <TableRow>
                            <TableCell padding="checkbox">
                              <Checkbox
                                indeterminate={s3SelectedKeys.size > 0 && s3SelectedKeys.size < s3Files.length}
                                checked={s3Files.length > 0 && s3SelectedKeys.size === s3Files.length}
                                onChange={handleToggleAllS3Files}
                                size="small"
                              />
                            </TableCell>
                            <TableCell sx={{ fontWeight: 600 }}>File</TableCell>
                            <TableCell sx={{ fontWeight: 600, width: 100 }}>Size</TableCell>
                            <TableCell sx={{ fontWeight: 600, width: 180 }}>Last Modified</TableCell>
                          </TableRow>
                        </TableHead>
                        <TableBody>
                          {s3Files.map((file) => (
                            <TableRow
                              key={file.key}
                              hover
                              onClick={() => handleToggleS3File(file.key)}
                              sx={{ cursor: 'pointer' }}
                              selected={s3SelectedKeys.has(file.key)}
                            >
                              <TableCell padding="checkbox">
                                <Checkbox checked={s3SelectedKeys.has(file.key)} size="small" />
                              </TableCell>
                              <TableCell>
                                <Typography variant="body2" sx={{ fontFamily: 'monospace', fontSize: '0.8rem', wordBreak: 'break-all' }}>
                                  {file.key}
                                </Typography>
                              </TableCell>
                              <TableCell>
                                <Typography variant="body2" color="text.secondary" sx={{ fontSize: '0.8rem', whiteSpace: 'nowrap' }}>
                                  {formatFileSize(file.size)}
                                </Typography>
                              </TableCell>
                              <TableCell>
                                <Typography variant="body2" color="text.secondary" sx={{ fontSize: '0.8rem', whiteSpace: 'nowrap' }}>
                                  {new Date(file.last_modified).toLocaleString()}
                                </Typography>
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </TableContainer>
                  </>
                )}

                {s3IngestResults.length > 0 && (
                  <Box sx={{ mt: 2 }}>
                    <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>Ingestion Results</Typography>
                    {s3IngestResults.map((r, i) => (
                      <Alert key={i} severity={r.success ? 'success' : 'error'} sx={{ mb: 0.5, py: 0, '& .MuiAlert-message': { fontSize: '0.8rem' } }}>
                        <strong>{r.key.split('/').pop()}</strong>: {r.message}
                      </Alert>
                    ))}
                  </Box>
                )}
              </Box>
            )}
          </TabPanel>
        )

      case 'integrations': {
        const searchLower = mcpSearch.toLowerCase()
        const filterBySearch = (servers: string[]) =>
          searchLower
            ? servers.filter(n =>
                n.toLowerCase().includes(searchLower) ||
                (SERVER_DESCRIPTIONS[n] || '').toLowerCase().includes(searchLower)
              )
            : servers
        const filterIntegrationsBySearch = (integrations: IntegrationMetadata[]) =>
          searchLower
            ? integrations.filter(i =>
                i.name.toLowerCase().includes(searchLower) ||
                i.id.toLowerCase().includes(searchLower) ||
                (i.description || '').toLowerCase().includes(searchLower) ||
                (i.category || '').toLowerCase().includes(searchLower)
              )
            : integrations

        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            {/* Header */}
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
              <Box>
                <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Integrations</Typography>
                <Typography variant="caption" color="text.secondary">
                  Toggle integrations on/off, configure API credentials, and access vendor documentation.
                </Typography>
              </Box>
              <Box sx={{ display: 'flex', gap: 1 }}>
                <Button size="small" variant="outlined" startIcon={<AddIcon />} onClick={() => setCustomIntegrationBuilderOpen(true)}>Build Custom</Button>
                <Button size="small" startIcon={<RefreshIcon />} onClick={loadMcpServers}>Refresh</Button>
              </Box>
            </Box>

            {/* Summary chips + Search */}
            <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2, flexWrap: 'wrap', gap: 1.5 }}>
              <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
                <Chip label={`${Object.values(mcpEnabled).filter(Boolean).length} Enabled`} color="primary" size="small" variant="outlined" />
                <Chip label={`${Object.values(mcpStatuses).filter(s => s === 'running').length} Running`} color="success" size="small" variant="outlined" />
                <Chip label={`${integrationsConfig.enabled_integrations.length} Configured`} color="info" size="small" variant="outlined" />
                <Chip label={`${mcpServers.length} Active`} size="small" variant="outlined" />
                {totalUnimplemented > 0 && (
                  <Chip label={`${totalUnimplemented} Planned`} size="small" variant="outlined" sx={{ borderColor: '#ff9800', color: '#ff9800' }} />
                )}
                <Chip label={`${mcpServers.length + totalUnimplemented} Total`} size="small" variant="outlined" />
              </Box>
              <TextField
                size="small"
                placeholder="Search integrations…"
                value={mcpSearch}
                onChange={(e) => setMcpSearch(e.target.value)}
                InputProps={{
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchIcon sx={{ fontSize: 18, color: 'text.disabled' }} />
                    </InputAdornment>
                  ),
                }}
                sx={{ minWidth: 220, '& .MuiOutlinedInput-root': { borderRadius: 2 } }}
              />
            </Box>

            <Box>
              {MCP_CATEGORIES.map(category => {
                const servers = filterBySearch(mcpServers.filter(category.filter))
                const wipIntegrations = filterIntegrationsBySearch(unimplementedByMcpCat[category.label] || [])
                if (servers.length === 0 && wipIntegrations.length === 0) return null
                const enabledCount = servers.filter(n => mcpEnabled[n]).length
                const totalInCategory = servers.length + wipIntegrations.length
                return (
                  <Accordion
                    key={category.label}
                    disableGutters
                    elevation={0}
                    sx={{
                      border: '1px solid',
                      borderColor: 'divider',
                      borderRadius: '8px !important',
                      mb: 1.5,
                      '&:before': { display: 'none' },
                      overflow: 'hidden',
                    }}
                  >
                    <AccordionSummary
                      expandIcon={<ExpandMoreIcon />}
                      sx={{
                        minHeight: 44,
                        px: 2,
                        '& .MuiAccordionSummary-content': { my: 0.75, alignItems: 'center', gap: 1 },
                      }}
                    >
                      <Typography variant="body2" sx={{ fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', fontSize: '0.7rem', color: 'text.secondary' }}>
                        {category.label}
                      </Typography>
                      <Chip label={`${enabledCount}/${totalInCategory}`} size="small" sx={{ height: 20, fontSize: '0.65rem', fontWeight: 600 }} />
                      {wipIntegrations.length > 0 && (
                        <Chip label={`${wipIntegrations.length} planned`} size="small" sx={{ height: 20, fontSize: '0.6rem', fontWeight: 600, bgcolor: alpha('#ff9800', 0.1), color: '#ff9800' }} />
                      )}
                    </AccordionSummary>
                    <AccordionDetails sx={{ px: 2, pt: 0, pb: 2 }}>
                      <Grid container spacing={2}>
                        {servers.map(renderServerCard)}
                        {wipIntegrations.map(renderUnimplementedCard)}
                      </Grid>
                    </AccordionDetails>
                  </Accordion>
                )
              })}

              {(() => {
                const uncategorized = filterBySearch(mcpServers.filter(n => !CATEGORIZED_NAMES.has(n)))
                const wipOther = filterIntegrationsBySearch(unimplementedByMcpCat['Other'] || [])
                if (uncategorized.length === 0 && wipOther.length === 0) return null
                const enabledCount = uncategorized.filter(n => mcpEnabled[n]).length
                const totalInCategory = uncategorized.length + wipOther.length
                return (
                  <Accordion
                    disableGutters
                    elevation={0}
                    sx={{
                      border: '1px solid',
                      borderColor: 'divider',
                      borderRadius: '8px !important',
                      mb: 1.5,
                      '&:before': { display: 'none' },
                      overflow: 'hidden',
                    }}
                  >
                    <AccordionSummary
                      expandIcon={<ExpandMoreIcon />}
                      sx={{
                        minHeight: 44,
                        px: 2,
                        '& .MuiAccordionSummary-content': { my: 0.75, alignItems: 'center', gap: 1 },
                      }}
                    >
                      <Typography variant="body2" sx={{ fontWeight: 700, letterSpacing: 0.5, textTransform: 'uppercase', fontSize: '0.7rem', color: 'text.secondary' }}>
                        Other
                      </Typography>
                      <Chip label={`${enabledCount}/${totalInCategory}`} size="small" sx={{ height: 20, fontSize: '0.65rem', fontWeight: 600 }} />
                    </AccordionSummary>
                    <AccordionDetails sx={{ px: 2, pt: 0, pb: 2 }}>
                      <Grid container spacing={2}>
                        {uncategorized.map(renderServerCard)}
                        {wipOther.map(renderUnimplementedCard)}
                      </Grid>
                    </AccordionDetails>
                  </Accordion>
                )
              })()}

              {searchLower && filterBySearch(mcpServers).length === 0 && filterIntegrationsBySearch(getAllIntegrations()).length === 0 && (
                <Alert severity="info" sx={{ mt: 2 }}>No integrations match "{mcpSearch}"</Alert>
              )}
            </Box>

          </TabPanel>
        )
      }

      case 'dev':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 0.5 }}>Developer Tools</Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
              Infrastructure and local-environment settings for development.
            </Typography>

            {/* ---- PostgreSQL ---- */}
            <Card variant="outlined" sx={{ mb: 3, borderColor: 'primary.main', borderWidth: 1 }}>
              <CardContent>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle2" sx={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 1 }}>
                      PostgreSQL
                    </Typography>
                    {storageStatus && (
                      <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                        <Chip label={storageStatus.backend === 'postgresql' ? 'PostgreSQL Active' : 'JSON Files'} color={storageStatus.backend === 'postgresql' ? 'success' : 'warning'} size="small" />
                        {storageHealth && <Chip label={`${storageHealth.findings_count} Findings, ${storageHealth.cases_count} Cases`} size="small" variant="outlined" />}
                      </Box>
                    )}
                  </Box>
                  <Box sx={{ display: 'flex', gap: 1 }}>
                    <Button size="small" startIcon={<RefreshIcon />} onClick={loadStorageStatus}>Refresh</Button>
                    <Button size="small" variant="contained" startIcon={<RefreshIcon />} onClick={handleReconnectDatabase} disabled={reconnecting}>{reconnecting ? 'Reconnecting...' : 'Reconnect'}</Button>
                  </Box>
                </Box>
                {storageStatus && storageStatus.backend !== 'postgresql' && (
                  <Alert severity="info" sx={{ mb: 2 }}>Start PostgreSQL to enable database storage: <code>./scripts/start_database.sh</code></Alert>
                )}
                <Box sx={{ maxWidth: 500 }}>
                  <TextField fullWidth label="Connection String" type="password" value={postgresqlConfig.connection_string} onChange={(e) => setPostgresqlConfig({ ...postgresqlConfig, connection_string: e.target.value })} placeholder="postgresql://user:pass@localhost:5432/db" sx={{ mb: 2 }} />
                  <Button variant="contained" startIcon={<SaveIcon />} onClick={handleSavePostgreSQL}>Save</Button>
                </Box>
              </CardContent>
            </Card>

            {/* ---- Local Splunk Enterprise ---- */}
            <Card variant="outlined" sx={{ mb: 2, borderColor: 'primary.main', borderWidth: 1 }}>
              <CardContent>
                <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 2, display: 'flex', alignItems: 'center', gap: 1 }}>
                  <Box component="span" sx={{ width: 8, height: 8, borderRadius: '50%', bgcolor: splunkStatus?.running ? 'success.main' : 'grey.500' }} />
                  Local Splunk Enterprise
                </Typography>
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                  {splunkStatus && (
                    <>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                        <Typography variant="body2" color="text.secondary">
                          Status: <strong>{splunkStatus.running ? 'Running' : 'Stopped'}</strong>
                        </Typography>
                        {splunkStatus.running && splunkStatus.web_url && (
                          <Button size="small" variant="outlined" startIcon={<OpenInNewIcon />} onClick={() => window.open(splunkStatus.web_url, '_blank')} sx={{ textTransform: 'none' }}>
                            Open Splunk UI
                          </Button>
                        )}
                      </Box>
                      {splunkStatus.running && (
                        <Alert severity="info" sx={{ py: 0.5 }}>
                          <Typography variant="caption">
                            <strong>Web UI:</strong> {splunkStatus.web_url} |{' '}
                            <strong>HEC:</strong> {splunkStatus.hec_url} |{' '}
                            <strong>User:</strong> {splunkStatus.username} |{' '}
                            <strong>Pass:</strong> {splunkStatus.note?.split(': ')[1] || 'changeme123'}
                          </Typography>
                        </Alert>
                      )}
                    </>
                  )}
                  <Box sx={{ display: 'flex', gap: 1 }}>
                    {!splunkStatus?.running ? (
                      <Button size="small" variant="contained" startIcon={<StartIcon />} onClick={handleStartSplunk} disabled={splunkLoading}>Start Splunk</Button>
                    ) : (
                      <>
                        <Button size="small" variant="outlined" startIcon={<StopIcon />} onClick={handleStopSplunk} disabled={splunkLoading} color="error">Stop</Button>
                        <Button size="small" variant="outlined" startIcon={<RefreshIcon />} onClick={handleRestartSplunk} disabled={splunkLoading}>Restart</Button>
                      </>
                    )}
                    <Button size="small" variant="text" startIcon={<RefreshIcon />} onClick={loadSplunkStatus} disabled={splunkLoading}>Refresh Status</Button>
                  </Box>
                </Box>
              </CardContent>
            </Card>
          </TabPanel>
        )

      case 'users':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <UserManagementTab />
          </TabPanel>
        )

      case 'agents':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <AgentBuilderTab onMessage={setMessage} showConfirm={showConfirm} />
          </TabPanel>
        )

      case 'autoinvestigate':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <Box sx={{ maxWidth: 800 }}>
              <AutoInvestigateTab onMessage={setMessage} showConfirm={showConfirm} />
            </Box>
          </TabPanel>
        )

      case 'skills':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <SkillsTab />
          </TabPanel>
        )

      case 'kafka':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <Box sx={{ maxWidth: 900 }}>
              <KafkaTab onMessage={setMessage} />
            </Box>
          </TabPanel>
        )

      case 'general':
        return (
          <TabPanel value={currentTab} index={idx} key={tabKey}>
            <Box sx={{ maxWidth: 400 }}>
              <Typography variant="subtitle1" sx={{ fontWeight: 600, mb: 2 }}>General</Typography>
              <FormGroup>
                <FormControlLabel control={<Switch checked={generalConfig.auto_start_sync} onChange={(e) => setGeneralConfig({ ...generalConfig, auto_start_sync: e.target.checked })} />} label="Auto-sync on start" />
                <FormControlLabel control={<Switch checked={generalConfig.show_notifications} onChange={(e) => handleNotificationToggle(e.target.checked)} />} label="Desktop notifications" />
                <FormControlLabel control={<Switch checked={generalConfig.enable_keyring} onChange={(e) => setGeneralConfig({ ...generalConfig, enable_keyring: e.target.checked })} />} label="Use OS Keyring" />
              </FormGroup>
              <Divider sx={{ my: 2 }} />
              <Button variant="contained" startIcon={<SaveIcon />} onClick={handleSaveGeneral}>Save</Button>
            </Box>
          </TabPanel>
        )

      default:
        return null
    }
  }

  return (
    <Box>
      <Box mb={3}>
        <Typography variant="h5" sx={{ fontWeight: 700, mb: 0.5 }}>Settings</Typography>
        <Typography variant="body2" color="text.secondary">Configure integrations and preferences</Typography>
      </Box>

      {message && (
        <Alert severity={message.type} sx={{ mb: 2 }} onClose={() => setMessage(null)}>
          {message.text}
        </Alert>
      )}

      <Box sx={{ bgcolor: 'background.paper', borderRadius: 3, border: 1, borderColor: 'divider' }}>
        <Box sx={{ borderBottom: 1, borderColor: 'divider', px: 1 }}>
          <Tabs value={currentTab} onChange={(_, v) => setCurrentTab(v)} variant="scrollable" scrollButtons="auto">
            {visibleTabs.map(t => (
              <Tab key={t.key} label={t.label} sx={{ minHeight: 48 }} />
            ))}
          </Tabs>
        </Box>

        {visibleTabs.map((t, i) => renderTabContent(t.key, i))}
      </Box>

      {/* ---- Dialogs ---- */}
      {selectedIntegration && (
        <IntegrationWizard
          open={wizardOpen}
          onClose={() => setWizardOpen(false)}
          integration={getAllIntegrations().find(i => i.id === selectedIntegration)!}
          onSave={handleSaveIntegration}
          existingConfig={integrationsConfig.integrations[selectedIntegration]}
        />
      )}

      {customIntegrationBuilderOpen && (
        <CustomIntegrationBuilder
          onClose={() => setCustomIntegrationBuilderOpen(false)}
          onSave={(integrationId) => { setCustomIntegrationBuilderOpen(false); setMessage({ type: 'success', text: `Custom integration '${integrationId}' created` }); loadConfigs() }}
        />
      )}

      <Dialog open={confirmDialog.open} onClose={handleConfirmClose}>
        <DialogTitle>{confirmDialog.title}</DialogTitle>
        <DialogContent><DialogContentText>{confirmDialog.message}</DialogContentText></DialogContent>
        <DialogActions>
          <Button onClick={handleConfirmClose} color="inherit">Cancel</Button>
          <Button onClick={handleConfirmAction} variant="contained" autoFocus>Confirm</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={s3HelpOpen} onClose={() => setS3HelpOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle>How to Configure Your S3 Bucket in AWS</DialogTitle>
        <DialogContent>
          <Box sx={{ '& > *': { mb: 3 } }}>
            <Alert severity="info">Follow these steps to create and configure an S3 bucket for Vigil SOC to store findings and cases.</Alert>
            <Box>
              <Typography variant="h6" gutterBottom>Step 1: Create an S3 Bucket</Typography>
              <Typography variant="body2" component="div">
                1. Sign in to the <a href="https://console.aws.amazon.com/s3/" target="_blank" rel="noopener noreferrer">AWS S3 Console</a><br/>
                2. Click <strong>"Create bucket"</strong><br/>
                3. Enter a unique bucket name (e.g., <code>deeptempo-soc-data</code>)<br/>
                4. Select your preferred AWS Region<br/>
                5. Keep "Block all public access" enabled (recommended)<br/>
                6. Click <strong>"Create bucket"</strong>
              </Typography>
            </Box>
            <Box>
              <Typography variant="h6" gutterBottom>Step 2: Create an IAM User</Typography>
              <Typography variant="body2" component="div">
                1. Go to the <a href="https://console.aws.amazon.com/iam/" target="_blank" rel="noopener noreferrer">AWS IAM Console</a><br/>
                2. Click <strong>"Users"</strong> → <strong>"Create user"</strong><br/>
                3. Enter username (e.g., <code>deeptempo-s3-access</code>)<br/>
                4. Click <strong>"Next"</strong> → Select <strong>"Attach policies directly"</strong> → Click <strong>"Create policy"</strong>
              </Typography>
            </Box>
            <Box>
              <Typography variant="h6" gutterBottom>Step 3: Create IAM Policy</Typography>
              <Typography variant="body2" gutterBottom>Click <strong>"JSON"</strong> and paste (replace <code>YOUR-BUCKET-NAME</code>):</Typography>
              <Box sx={{ bgcolor: 'background.default', p: 2, borderRadius: 1, border: '1px solid', borderColor: 'divider', fontFamily: 'monospace', fontSize: '0.75rem', overflow: 'auto' }}>
                {`{\n  "Version": "2012-10-17",\n  "Statement": [{\n    "Sid": "DeepTempoS3Access",\n    "Effect": "Allow",\n    "Action": ["s3:GetObject","s3:PutObject","s3:ListBucket","s3:DeleteObject"],\n    "Resource": ["arn:aws:s3:::YOUR-BUCKET-NAME","arn:aws:s3:::YOUR-BUCKET-NAME/*"]\n  }]\n}`}
              </Box>
              <Typography variant="body2" sx={{ mt: 1 }}>Name the policy <code>DeepTempoS3Policy</code> and create it.</Typography>
            </Box>
            <Box>
              <Typography variant="h6" gutterBottom>Step 4–6: Attach Policy, Create Access Keys, Configure</Typography>
              <Typography variant="body2" component="div">
                Attach the policy to your user, generate access keys, then enter the Bucket Name, Region, Access Key ID, and Secret Access Key in the form above.
              </Typography>
            </Box>
            <Alert severity="warning">
              <strong>Security:</strong> Never share credentials. Use least privilege. Rotate keys regularly.
            </Alert>
          </Box>
        </DialogContent>
        <DialogActions><Button onClick={() => setS3HelpOpen(false)}>Close</Button></DialogActions>
      </Dialog>

      {/* Detection Rules Dialog (opened from security-detections card) */}
      <Dialog open={detectionRulesOpen} onClose={() => setDetectionRulesOpen(false)} maxWidth="lg" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Typography variant="h6" sx={{ fontWeight: 600 }}>Security Detections — Rule Sources</Typography>
          <Button onClick={() => setDetectionRulesOpen(false)} size="small">Close</Button>
        </DialogTitle>
        <DialogContent dividers>
          <DetectionRulesTab />
        </DialogContent>
      </Dialog>
    </Box>
  )
}
