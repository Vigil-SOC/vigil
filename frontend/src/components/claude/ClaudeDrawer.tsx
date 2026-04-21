import { useState, useRef, useEffect } from 'react'
import {
  Drawer,
  Box,
  Typography,
  TextField,
  Button,
  IconButton,
  CircularProgress,
  Tabs,
  Tab,
  Collapse,
  FormControl,
  Select,
  MenuItem,
  Chip,
  LinearProgress,
  List,
  ListItem,
  ListItemText,
  ListItemSecondaryAction,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Tooltip,
  ClickAwayListener,
  Switch,
  alpha,
  useTheme,
} from '@mui/material'
import {
  Send as SendIcon,
  Add as AddIcon,
  Close as CloseIcon,
  Settings as SettingsIcon,
  AttachFile as AttachFileIcon,
  Image as ImageIcon,
  Delete as DeleteIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  PictureAsPdf as PdfIcon,
  Info as InfoIcon,
  Refresh as RefreshIcon,
  Compress as CompressIcon,
  Warning as WarningIcon,
  Psychology as ReasoningIcon,
  ExpandMore as ExpandMoreIcon,
  ExpandLess as ExpandLessIcon,
} from '@mui/icons-material'
import { claudeApi, agentsApi, mcpApi, reasoningApi } from '../../services/api'
import { notificationService } from '../../services/notifications'
import { createLogger } from '../../services/logger'

const logger = createLogger('ClaudeDrawer')

interface ContentBlock {
  type: 'text' | 'image' | 'thinking'
  text?: string
  source?: { type: 'base64'; media_type: string; data: string }
}

interface Message {
  role: 'user' | 'assistant'
  content: string | ContentBlock[]
}

interface ChatTab {
  id: string
  title: string
  messages: Message[]
  investigationKey?: string
}

interface ClaudeDrawerProps {
  open: boolean
  onClose: () => void
  initialMessages?: Message[]
  initialAgentId?: string
  initialTitle?: string
}

interface Agent {
  id: string
  name: string
  description: string
  icon?: string
  color?: string
  specialization?: string
}

interface AttachedFile {
  name: string
  type: 'image' | 'text' | 'file'
  data: string
  media_type?: string
}

export default function ClaudeDrawer({ open, onClose, initialMessages, initialAgentId, initialTitle }: ClaudeDrawerProps) {
  const theme = useTheme()
  
  const stripThinkingBlocks = (messages: Message[]): Message[] => {
    return messages.map(msg => {
      if (msg.role === 'assistant' && Array.isArray(msg.content)) {
        const filtered = msg.content.filter((b: any) => b.type !== 'thinking')
        if (filtered.length === 0) return { ...msg, content: '' }
        return { ...msg, content: filtered }
      }
      return msg
    }).filter(msg => !(msg.role === 'assistant' && msg.content === ''))
  }

  const loadPersistedData = () => {
    try {
      const savedTabs = localStorage.getItem('claudeDrawerTabs')
      const savedTab = localStorage.getItem('claudeDrawerCurrentTab')
      if (savedTabs) {
        const parsed = JSON.parse(savedTabs)
        // Don't filter out thinking blocks - preserve them so they can be displayed
        return { tabs: parsed, currentTab: savedTab ? parseInt(savedTab, 10) : 0 }
      }
    } catch { /* Ignore localStorage errors */ }
    return { tabs: [{ id: '1', title: 'Chat 1', messages: [] }], currentTab: 0 }
  }

  const loadPersistedSettings = () => {
    try {
      const saved = localStorage.getItem('claudeDrawerSettings')
      if (saved) {
        const parsed = JSON.parse(saved)
        logger.debug('Settings loaded', parsed)
        return parsed
      }
    } catch (e) {
      logger.error('Failed to load settings', e)
    }
    return {
      model: 'claude-sonnet-4-20250514', 
      maxTokens: 4096, 
      systemPrompt: '', 
      selectedAgent: '',
      enableThinking: false,
      thinkingBudget: 10000
    }
  }

  const persisted = loadPersistedData()
  const settings = loadPersistedSettings()
  
  const [tabs, setTabs] = useState<ChatTab[]>(persisted.tabs)
  const [currentTab, setCurrentTab] = useState(persisted.currentTab)
  const [lastInvestigationId, setLastInvestigationId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [model, setModel] = useState(settings.model)
  const [maxTokens, setMaxTokens] = useState(settings.maxTokens)
  const [enableThinking, setEnableThinking] = useState<boolean>(settings.enableThinking ?? false)
  const [thinkingBudget, setThinkingBudget] = useState<number>(settings.thinkingBudget ?? 10000)
  const [systemPrompt, setSystemPrompt] = useState(settings.systemPrompt)
  const [agents, setAgents] = useState<Agent[]>([])
  const [selectedAgent, setSelectedAgent] = useState<string>(settings.selectedAgent)
  const [agentInfoDialogOpen, setAgentInfoDialogOpen] = useState(false)
  const [models, setModels] = useState<any[]>([])
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [mcpStatus, setMcpStatus] = useState<{ available: number; total: number } | null>(null)
  const [estimatedTokens, setEstimatedTokens] = useState(0)
  const [streamingThinking, setStreamingThinking] = useState<string>('')
  const [isThinking, setIsThinking] = useState(false)
  const [streamingText, setStreamingText] = useState<string>('')
  const [summarizing, setSummarizing] = useState(false)
  // GH #79 — Reasoning trace state
  const [sessionSummary, setSessionSummary] = useState<{
    total_interactions: number
    total_cost_usd: number
    total_input_tokens: number
    total_output_tokens: number
  } | null>(null)
  const [traceOpen, setTraceOpen] = useState(false)
  const [traceInteractions, setTraceInteractions] = useState<any[]>([])
  const [traceSelected, setTraceSelected] = useState<any | null>(null)
  const [traceLoading, setTraceLoading] = useState(false)
  const [collapsedThinking, setCollapsedThinking] = useState<Record<string, boolean>>({})
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const scrollToBottom = () => messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })

  useEffect(() => { scrollToBottom() }, [tabs, currentTab])
  useEffect(() => { try { localStorage.setItem('claudeDrawerTabs', JSON.stringify(tabs)) } catch { /* ignore */ } }, [tabs])
  
  // Debug logging for messages (only in development)
  useEffect(() => {
    if (tabs[currentTab] && import.meta.env.DEV) {
      logger.debug('Messages updated', {
        tabId: tabs[currentTab].id,
        messageCount: tabs[currentTab].messages.length
      })
    }
  }, [tabs, currentTab])
  useEffect(() => { try { localStorage.setItem('claudeDrawerCurrentTab', currentTab.toString()) } catch { /* ignore */ } }, [currentTab])

  // GH #79 — load reasoning-trace summary when switching tabs
  useEffect(() => {
    const sid = tabs[currentTab]?.id
    if (!sid) { setSessionSummary(null); return }
    reasoningApi.getSessionSummary(sid)
      .then(s => setSessionSummary({
        total_interactions: s.total_interactions,
        total_cost_usd: s.total_cost_usd,
        total_input_tokens: s.total_input_tokens,
        total_output_tokens: s.total_output_tokens,
      }))
      .catch(() => setSessionSummary(null))
  }, [currentTab, tabs])

  const openReasoningTrace = async () => {
    const sid = tabs[currentTab]?.id
    if (!sid) return
    setTraceOpen(true)
    setTraceLoading(true)
    setTraceSelected(null)
    try {
      const resp = await reasoningApi.listInteractions(sid, { limit: 200 })
      setTraceInteractions(resp.interactions || [])
    } catch (e) {
      logger.error('Failed to load reasoning trace', e)
      setTraceInteractions([])
    } finally {
      setTraceLoading(false)
    }
  }

  const loadTraceInteraction = async (interactionId: string) => {
    const sid = tabs[currentTab]?.id
    if (!sid) return
    try {
      const detail = await reasoningApi.getInteraction(sid, interactionId)
      setTraceSelected(detail)
    } catch (e) {
      logger.error('Failed to load interaction detail', e)
    }
  }

  const toggleThinking = (key: string) => {
    setCollapsedThinking(prev => ({ ...prev, [key]: !prev[key] }))
  }

  useEffect(() => {
    try {
      const settingsToSave = {
        model, 
        maxTokens,
        enableThinking,
        thinkingBudget,
        systemPrompt, 
        selectedAgent
      }
      localStorage.setItem('claudeDrawerSettings', JSON.stringify(settingsToSave))
      logger.debug('Settings saved', settingsToSave)
    } catch (e) {
      logger.error('Failed to save settings', e)
    }
  }, [model, maxTokens, enableThinking, thinkingBudget, systemPrompt, selectedAgent])

  useEffect(() => {
    if (open) {
      agentsApi.listAgents().then(res => setAgents(res.data.agents || [])).catch(() => {})
      claudeApi.getModels().then(res => setModels(res.data.models || [])).catch(() => {})
      mcpApi.getStatuses().then(res => {
        const statuses = res.data.statuses || []
        const available = statuses.filter((s: any) => s.status && s.status !== 'error' && s.status !== 'not found').length
        setMcpStatus({ available, total: statuses.length })
      }).catch(() => {})
    }
  }, [open])

  useEffect(() => {
    const investigationId = initialMessages && initialAgentId ? `${initialAgentId}-${JSON.stringify(initialMessages)}` : null
    if (open && initialMessages?.length && initialAgentId && investigationId !== lastInvestigationId) {
      setLastInvestigationId(investigationId)
      let findingId = ''
      const content = typeof initialMessages[0]?.content === 'string' ? initialMessages[0].content : ''
      const match = content.match(/f-\d{8}-[a-f0-9]{8}/i)
      if (match) findingId = match[0]
      const key = findingId ? `${findingId}-${initialAgentId}` : null
      const existingIdx = key ? tabs.findIndex(t => t.investigationKey === key) : -1
      if (existingIdx !== -1) {
        setCurrentTab(existingIdx)
        setSelectedAgent(initialAgentId)
        return
      }
      const newTab: ChatTab = { id: `inv-${Date.now()}`, title: initialTitle || 'Investigation', messages: initialMessages, investigationKey: key || undefined }
      setTabs(prev => [...prev, newTab])
      setCurrentTab(tabs.length)
      setSelectedAgent(initialAgentId)
      setLoading(true)
      setStreamingThinking('')
      setStreamingText('')
      setIsThinking(false)
      setTimeout(async () => {
        try {
          logger.investigate('Starting auto-investigation (streaming)', {
            agentId: initialAgentId,
            messageCount: initialMessages.length
          })

          const response = await fetch('/api/claude/chat/stream', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Accept': 'text/event-stream',
            },
            body: JSON.stringify({
              messages: initialMessages,
              model: model || 'claude-sonnet-4-20250514',
              max_tokens: maxTokens,
              enable_thinking: enableThinking,
              thinking_budget: enableThinking ? thinkingBudget : undefined,
              agent_id: initialAgentId,
            }),
          })

          if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`)

          const reader = response.body?.getReader()
          const decoder = new TextDecoder()
          const thinkingContent: ContentBlock[] = []
          const textContent: ContentBlock[] = []
          let currentThinking = ''
          let currentText = ''

          if (reader) {
            try {
              while (true) {
                const { done, value } = await reader.read()
                if (done) break
                const chunk = decoder.decode(value)
                for (const line of chunk.split('\n')) {
                  if (!line.startsWith('data: ')) continue
                  const data = line.slice(6).trim()
                  if (!data) continue
                  let event: any
                  try { event = JSON.parse(data) } catch { continue }
                  if (event.error) {
                    throw new Error(event.error)
                  } else if (event.type === 'thinking_start') {
                    setIsThinking(true)
                    currentThinking = ''
                  } else if (event.type === 'thinking') {
                    currentThinking += event.content
                    setStreamingThinking(currentThinking)
                  } else if (event.type === 'thinking_end') {
                    setIsThinking(false)
                    if (currentThinking) thinkingContent.push({ type: 'thinking', text: currentThinking })
                  } else if (event.type === 'text') {
                    currentText += event.content
                    setStreamingText(currentText)
                  }
                }
              }
            } finally {
              reader.releaseLock()
            }
          }

          if (currentText) textContent.push({ type: 'text', text: currentText })
          const responseContent: ContentBlock[] = [...thinkingContent, ...textContent]

          setTabs(prev => prev.map(t =>
            t.id === newTab.id
              ? { ...t, messages: [...initialMessages, { role: 'assistant' as const, content: responseContent }] }
              : t
          ))
          setStreamingThinking('')
          setStreamingText('')
          setIsThinking(false)
          notificationService.notifyInvestigationComplete({ title: initialTitle || 'Investigation', summary: 'Analysis complete' })
        } catch (e: any) {
          logger.error('Investigation streaming error', e)
          setTabs(prev => prev.map(t =>
            t.id === newTab.id
              ? { ...t, messages: [...initialMessages, { role: 'assistant', content: `Error: ${e?.message || 'Failed'}` }] }
              : t
          ))
          setStreamingThinking('')
          setStreamingText('')
          setIsThinking(false)
        } finally { setLoading(false) }
      }, 300)
    }
  }, [open, initialMessages, initialAgentId, initialTitle, lastInvestigationId])

  useEffect(() => { if (!open) setLastInvestigationId(null) }, [open])

  useEffect(() => {
    const msgs = tabs[currentTab]?.messages || []
    let total = 0
    msgs.forEach(msg => {
      if (typeof msg.content === 'string') total += msg.content.length / 4
      else msg.content.forEach(b => { if (b.type === 'text' && b.text) total += b.text.length / 4; else if (b.type === 'image') total += 85 })
    })
    total += input.length / 4 + (systemPrompt?.length || 0) / 4
    setEstimatedTokens(Math.round(total))
  }, [tabs, currentTab, input, systemPrompt])

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files) return
    for (let i = 0; i < files.length; i++) {
      try {
        const res = await claudeApi.uploadFile(files[i])
        const d = res.data
        if (d.type === 'image') setAttachedFiles(prev => [...prev, { name: d.filename, type: 'image', data: d.data, media_type: d.media_type }])
        else if (d.type === 'text') setInput(prev => prev + '\n\n' + d.content)
      } catch { /* ignore file upload errors */ }
    }
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const handleSend = async () => {
    if ((!input.trim() && !attachedFiles.length) || loading) return
    
    const sessionId = `${Date.now()}-${Math.random().toString(36).substring(7)}`
    
    logger.send('=== OUTGOING MESSAGE ===', {
      sessionId,
      inputLength: input.length,
      attachedFiles: attachedFiles.length,
      model,
      selectedAgent,
      timestamp: new Date().toISOString()
    })
    
    let content: string | ContentBlock[]
    if (attachedFiles.length) {
      const blocks: ContentBlock[] = []
      if (input.trim()) blocks.push({ type: 'text', text: input.trim() })
      attachedFiles.forEach(f => { if (f.type === 'image' && f.media_type) blocks.push({ type: 'image', source: { type: 'base64', media_type: f.media_type, data: f.data } }) })
      content = blocks
    } else content = input.trim()
    const userMsg: Message = { role: 'user', content }
    
    const newTabs = [...tabs]
    newTabs[currentTab] = {
      ...newTabs[currentTab],
      messages: [...newTabs[currentTab].messages, userMsg]
    }
    
    // Strip thinking blocks from history before sending - backend/agent controls thinking
    const toSend = stripThinkingBlocks(newTabs[currentTab].messages)
    
    setTabs(newTabs)
    setInput('')
    setAttachedFiles([])
    setLoading(true)
    setStreamingThinking('')
    setStreamingText('')
    setIsThinking(false)
    
    try {
      logger.request('📤 === API REQUEST ===', {
        sessionId,
        messageCount: toSend.length,
        maxTokens,
        model,
        selectedAgent,
        timestamp: new Date().toISOString()
      })
      
      const response = await fetch('/api/claude/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'text/event-stream',
        },
        body: JSON.stringify({
          messages: toSend,
          model,
          max_tokens: maxTokens,
          enable_thinking: enableThinking,
          thinking_budget: enableThinking ? thinkingBudget : undefined,
          agent_id: selectedAgent || undefined,
          system_prompt: systemPrompt || undefined,
          session_id: tabs[currentTab]?.id,
        }),
      })
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }
      
      const reader = response.body?.getReader()
      const decoder = new TextDecoder()
      
      const thinkingContent: ContentBlock[] = []
      const textContent: ContentBlock[] = []
      let currentThinking = ''
      let currentText = ''
      
      if (reader) {
        try {
          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            
            const chunk = decoder.decode(value)
            const lines = chunk.split('\n')
            
            for (const line of lines) {
              if (line.startsWith('data: ')) {
                const data = line.slice(6)
                if (data.trim()) {
                  let event: any
                  try {
                    event = JSON.parse(data)
                  } catch (parseError) {
                    logger.error('Failed to parse SSE event JSON', { data, error: parseError })
                    continue
                  }
                  
                  if (event.error) {
                    throw new Error(event.error)
                  } else if (event.type === 'context_summarized') {
                    logger.info(`Context auto-summarized: ${event.summarized_messages} older messages condensed, ${event.remaining_messages} recent messages kept`)
                    currentText += `[Context auto-summarized: ${event.summarized_messages} older messages were condensed to preserve context within the model's limits. Recent messages and all key details are preserved.]\n\n`
                    setStreamingText(currentText)
                  } else if (event.type === 'thinking_start') {
                    setIsThinking(true)
                    currentThinking = ''
                    logger.receive('💭 Thinking started')
                  } else if (event.type === 'thinking') {
                    currentThinking += event.content
                    setStreamingThinking(currentThinking)
                  } else if (event.type === 'thinking_end') {
                    setIsThinking(false)
                    if (currentThinking) {
                      thinkingContent.push({ type: 'thinking', text: currentThinking })
                    }
                    logger.receive('💭 Thinking ended', { totalLength: currentThinking.length })
                  } else if (event.type === 'text') {
                    currentText += event.content
                    setStreamingText(currentText)
                  }
                }
              }
            }
          }
        } finally {
          reader.releaseLock()
        }
      }
      
      if (currentText) {
        textContent.push({ type: 'text', text: currentText })
      }
      
      const responseContent: ContentBlock[] = [...thinkingContent, ...textContent]
      
      logger.receive('📥 === RESPONSE COMPLETE ===', {
        sessionId,
        thinkingBlocks: thinkingContent.length,
        textBlocks: textContent.length,
        timestamp: new Date().toISOString()
      })
      
      setTabs(prevTabs => {
        const updatedTabs = [...prevTabs]
        updatedTabs[currentTab] = {
          ...updatedTabs[currentTab],
          messages: [...updatedTabs[currentTab].messages, { role: 'assistant', content: responseContent }]
        }
        return updatedTabs
      })
      
      setStreamingThinking('')
      setStreamingText('')
      setIsThinking(false)

      // GH #79 — refresh reasoning-trace summary for this session
      const sid = tabs[currentTab]?.id
      if (sid) {
        reasoningApi.getSessionSummary(sid)
          .then(s => setSessionSummary({
            total_interactions: s.total_interactions,
            total_cost_usd: s.total_cost_usd,
            total_input_tokens: s.total_input_tokens,
            total_output_tokens: s.total_output_tokens,
          }))
          .catch(() => { /* silent — trace persistence is best-effort */ })
      }
    } catch (e: any) {
      logger.error('❌ === API ERROR ===', {
        sessionId,
        error: e?.message || 'Unknown error',
        detail: e?.response?.data?.detail,
        status: e?.response?.status,
        timestamp: new Date().toISOString(),
        fullError: e
      })
      setTabs(prevTabs => {
        const updatedTabs = [...prevTabs]
        updatedTabs[currentTab] = {
          ...updatedTabs[currentTab],
          messages: [...updatedTabs[currentTab].messages, { role: 'assistant', content: `Error: ${e?.response?.data?.detail || e?.message || 'Failed'}` }]
        }
        return updatedTabs
      })
    } finally { setLoading(false) }
  }

  const handleKeyPress = (e: React.KeyboardEvent) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() } }
  const handleNewTab = () => { setTabs([...tabs, { id: `${Date.now()}`, title: `Chat ${tabs.length + 1}`, messages: [] }]); setCurrentTab(tabs.length) }
  const handleCloseTab = (idx: number) => {
    // If closing the last tab, create a new empty tab first to keep drawer open
    if (tabs.length === 1) {
      logger.info('Closing last tab - creating new empty tab')
      setTabs([{ id: `${Date.now()}`, title: 'Chat 1', messages: [] }])
      setCurrentTab(0)
      return
    }
    
    logger.info('Closing tab', { idx, totalTabs: tabs.length, currentTab })
    
    // Remove the tab
    const newTabs = tabs.filter((_, i) => i !== idx)
    setTabs(newTabs)
    
    // Adjust current tab if needed
    if (currentTab >= newTabs.length) {
      setCurrentTab(newTabs.length - 1)
    } else if (currentTab > idx) {
      setCurrentTab(currentTab - 1)
    }
  }

  const handleClearChat = () => {
    logger.info('Clearing current chat', { currentTab, tabId: tabs[currentTab].id })
    setTabs(prevTabs => {
      const newTabs = [...prevTabs]
      newTabs[currentTab] = {
        ...newTabs[currentTab],
        messages: []
      }
      return newTabs
    })
  }

  const handleSummarize = async () => {
    const currentMessages = tabs[currentTab]?.messages || []
    if (currentMessages.length < 4 || summarizing) return
    
    setSummarizing(true)
    logger.info('Summarizing conversation', { messageCount: currentMessages.length, estimatedTokens })
    
    try {
      const res = await claudeApi.summarizeConversation({
        messages: currentMessages,
        model
      })
      
      const summary = res.data.summary
      const savedTokens = res.data.estimated_tokens_saved
      const originalCount = res.data.original_message_count
      
      logger.success('Conversation summarized', { savedTokens, originalCount })
      
      // Replace conversation with a summary context message + note
      setTabs(prevTabs => {
        const newTabs = [...prevTabs]
        newTabs[currentTab] = {
          ...newTabs[currentTab],
          messages: [
            { role: 'user' as const, content: `[Previous conversation summarized - ${originalCount} messages condensed]\n\nPlease use the following context from our previous conversation to continue helping me:\n\n${summary}` },
            { role: 'assistant' as const, content: `I've reviewed the summary of our previous conversation (${originalCount} messages). I have full context of what we discussed, including all findings, cases, and analysis. How would you like to continue?` }
          ]
        }
        return newTabs
      })
      
      notificationService.notifyInvestigationComplete({
        title: 'Conversation Summarized',
        summary: `${originalCount} messages condensed, ~${Math.round(savedTokens / 1000)}k tokens freed`
      })
    } catch (e: any) {
      logger.error('Summarization failed', e)
      // Add error as a message so user sees it
      setTabs(prevTabs => {
        const newTabs = [...prevTabs]
        newTabs[currentTab] = {
          ...newTabs[currentTab],
          messages: [...newTabs[currentTab].messages, { role: 'assistant' as const, content: `Failed to summarize conversation: ${e?.response?.data?.detail || e?.message || 'Unknown error'}. You can try clearing the chat and starting fresh.` }]
        }
        return newTabs
      })
    } finally {
      setSummarizing(false)
    }
  }

  const renderContent = (content: string | ContentBlock[], messageIndex?: number) => {
    if (typeof content === 'string') {
      logger.render(`Rendering string content: ${content.length} chars`, { preview: content.substring(0, 100) })
      return <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{content}</Typography>
    }

    if (!Array.isArray(content)) {
      logger.error('Content is not string or array', { content, type: typeof content })
      return <Typography variant="body2" color="error">Invalid content type</Typography>
    }

    logger.render(`Rendering content blocks: ${content.length} blocks`)
    content.forEach((b, i) => {
      logger.debug(`Block ${i}: ${b.type}, ${b.text?.length || 0} chars`)
    })

    return <>{content.map((b, i) => {
      const thinkingKey = `${messageIndex ?? 'x'}-${i}`
      const collapsed = collapsedThinking[thinkingKey] ?? false
      return (
        <Box key={i} sx={{ mb: 1 }}>
          {b.type === 'text' && b.text && <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{b.text}</Typography>}
          {b.type === 'image' && b.source && <img src={`data:${b.source.media_type};base64,${b.source.data}`} alt="" style={{ maxWidth: '100%', borderRadius: 8, marginTop: 8 }} />}
          {b.type === 'thinking' && b.text && (
            <Box sx={{
              p: 1.5,
              borderRadius: 1,
              bgcolor: alpha(theme.palette.info.main, 0.05),
              borderLeft: 2,
              borderColor: 'info.main',
              mb: 1.5,
              mt: 0.5
            }}>
              <Box
                sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mb: collapsed ? 0 : 0.5, cursor: 'pointer' }}
                onClick={() => toggleThinking(thinkingKey)}
              >
                <Typography variant="caption" sx={{ fontWeight: 600, color: 'info.main', fontSize: '0.7rem', flex: 1 }}>
                  💭 THINKING ({b.text.length} chars)
                </Typography>
                {collapsed ? <ExpandMoreIcon sx={{ fontSize: 14, color: 'info.main' }} /> : <ExpandLessIcon sx={{ fontSize: 14, color: 'info.main' }} />}
              </Box>
              <Collapse in={!collapsed}>
                <Typography
                  variant="body2"
                  sx={{
                    whiteSpace: 'pre-wrap',
                    fontStyle: 'italic',
                    color: 'text.secondary',
                    fontSize: '0.85rem',
                    lineHeight: 1.5,
                    opacity: 0.9
                  }}
                >
                  {b.text}
                </Typography>
              </Collapse>
            </Box>
          )}
        </Box>
      )
    })}</>
  }

  return (
    <Drawer anchor="right" open={open} onClose={onClose} sx={{ '& .MuiDrawer-paper': { width: { xs: '100%', sm: 420, md: 480 }, bgcolor: 'background.default' } }}>
      <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
        <Box sx={{ p: 2, borderBottom: 1, borderColor: 'divider', display: 'flex', justifyContent: 'space-between', alignItems: 'center', bgcolor: 'background.paper' }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>Vigil</Typography>
            {sessionSummary && sessionSummary.total_interactions > 0 && (
              <Tooltip title={`${sessionSummary.total_interactions} LLM calls · in ${sessionSummary.total_input_tokens.toLocaleString()} tok · out ${sessionSummary.total_output_tokens.toLocaleString()} tok`}>
                <Chip
                  size="small"
                  label={`$${sessionSummary.total_cost_usd.toFixed(4)}`}
                  sx={{ height: 20, fontSize: '0.7rem' }}
                />
              </Tooltip>
            )}
          </Box>
          <Box>
            <Tooltip title="View reasoning trace">
              <span>
                <IconButton
                  size="small"
                  onClick={openReasoningTrace}
                  disabled={!sessionSummary || sessionSummary.total_interactions === 0}
                >
                  <ReasoningIcon sx={{ fontSize: 20 }} />
                </IconButton>
              </span>
            </Tooltip>
            <IconButton
              size="small"
              onClick={(e) => {
                e.stopPropagation()
                const newValue = !showSettings
                setShowSettings(newValue)
                logger.info('Settings button clicked', {
                  previousState: showSettings,
                  newState: newValue
                })
              }}
              sx={{
                bgcolor: showSettings ? alpha(theme.palette.primary.main, 0.15) : 'transparent',
                color: showSettings ? 'primary.main' : 'text.secondary',
                '&:hover': {
                  bgcolor: showSettings
                    ? alpha(theme.palette.primary.main, 0.25)
                    : alpha(theme.palette.text.primary, 0.05)
                }
              }}
            >
              <SettingsIcon sx={{ fontSize: 20 }} />
            </IconButton>
            <IconButton size="small" onClick={onClose}><CloseIcon sx={{ fontSize: 20 }} /></IconButton>
          </Box>
        </Box>

        <Collapse in={showSettings}>
          <ClickAwayListener 
            onClickAway={() => {
              // Don't close if clicking on the settings button itself
              setShowSettings(false)
              logger.debug('Click away detected, closing settings')
            }}
          >
            <Box sx={{ p: 2, bgcolor: alpha(theme.palette.background.paper, 0.5), borderBottom: 1, borderColor: 'divider', maxHeight: '70vh', overflowY: 'auto' }}>
              
              {/* Status Section */}
              <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 1, color: 'primary.main' }}>Status</Typography>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                <Typography variant="caption" sx={{ fontWeight: 600 }}>MCP Tools:</Typography>
                {mcpStatus ? <Chip icon={mcpStatus.available > 0 ? <CheckCircleIcon /> : <ErrorIcon />} label={`${mcpStatus.available}/${mcpStatus.total}`} size="small" color={mcpStatus.available > 0 ? 'success' : 'error'} /> : <CircularProgress size={14} />}
              </Box>
              <Box sx={{ mb: 2 }}>
                <Typography variant="caption" color={estimatedTokens > 150000 ? 'error.main' : estimatedTokens > 100000 ? 'warning.main' : 'text.secondary'}>
                  Context: ~{estimatedTokens.toLocaleString()} / 200,000 tokens
                  {estimatedTokens > 150000 && ' ⚠️ Auto-summarize on next send'}
                </Typography>
                <LinearProgress variant="determinate" value={Math.min((estimatedTokens / 200000) * 100, 100)} sx={{ height: 4, borderRadius: 2, mt: 0.5 }} color={estimatedTokens > 150000 ? 'error' : estimatedTokens > 100000 ? 'warning' : 'primary'} />
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.25 }}>
                  Output max: {maxTokens.toLocaleString()} tokens
                </Typography>
              </Box>

              {/* Model Settings Section */}
              <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 1, color: 'primary.main' }}>Model Settings</Typography>
              <FormControl fullWidth size="small" sx={{ mb: 1.5 }}>
                <Select value={model} onChange={(e) => setModel(e.target.value)} displayEmpty>
                  {models.map(m => <MenuItem key={m.id} value={m.id}>{m.name}</MenuItem>)}
                  {models.length === 0 && <MenuItem value="claude-sonnet-4-20250514">Claude 4 Sonnet</MenuItem>}
                </Select>
              </FormControl>
              <TextField 
                fullWidth 
                size="small" 
                type="number" 
                label="Max Tokens" 
                value={maxTokens} 
                onChange={(e) => setMaxTokens(parseInt(e.target.value) || 4096)} 
                sx={{ mb: 1.5 }} 
              />

              <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1 }}>
                <Typography variant="caption" sx={{ fontWeight: 600 }}>Extended Thinking</Typography>
                <Switch
                  size="small"
                  checked={enableThinking}
                  onChange={(e) => setEnableThinking(e.target.checked)}
                />
              </Box>
              {enableThinking && (
                <TextField
                  fullWidth
                  size="small"
                  type="number"
                  label="Thinking Budget (tokens)"
                  value={thinkingBudget}
                  onChange={(e) => setThinkingBudget(parseInt(e.target.value) || 10000)}
                  sx={{ mb: 1.5 }}
                  helperText="Max tokens Claude can use for reasoning"
                />
              )}

              {/* Agent Selection */}
              <FormControl fullWidth size="small" sx={{ mb: 2 }}>
                <Select
                  value={selectedAgent}
                  onChange={(e) => setSelectedAgent(e.target.value)}
                  displayEmpty
                  size="small"
                >
                  <MenuItem value="">No Agent (General Chat)</MenuItem>
                  {agents.map(agent => (
                    <MenuItem key={agent.id} value={agent.id}>
                      {agent.icon && `${agent.icon} `}{agent.name}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>

              {/* System Prompt Section */}
              <Typography variant="caption" sx={{ fontWeight: 700, display: 'block', mb: 1, mt: 2, color: 'primary.main' }}>Advanced</Typography>
              <TextField 
                fullWidth 
                size="small" 
                label="System Prompt (Optional)" 
                value={systemPrompt} 
                onChange={(e) => setSystemPrompt(e.target.value)} 
                multiline 
                rows={3} 
                placeholder="Override default system prompt..."
                sx={{ mb: 1 }} 
                helperText="Leave empty to use default prompt"
              />

              {/* Info Text */}
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 2, fontStyle: 'italic' }}>
                Settings are automatically saved
              </Typography>
            </Box>
          </ClickAwayListener>
        </Collapse>

        <Box sx={{ borderBottom: 1, borderColor: 'divider', display: 'flex', alignItems: 'center', bgcolor: 'background.paper' }}>
          <Tabs value={currentTab} onChange={(_, v) => setCurrentTab(v)} variant="scrollable" scrollButtons="auto" sx={{ flex: 1, minHeight: 40 }}>
            {tabs.map((tab, i) => (
              <Tab 
                key={tab.id} 
                sx={{ minHeight: 40, py: 0 }} 
                label={
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                    <Typography variant="caption">{tab.title}</Typography>
                    <Box
                      component="span"
                      onClick={(e) => { e.stopPropagation(); handleCloseTab(i) }}
                      sx={{ 
                        display: 'inline-flex', 
                        alignItems: 'center',
                        justifyContent: 'center',
                        p: 0.25,
                        borderRadius: '50%',
                        cursor: 'pointer',
                        '&:hover': { bgcolor: 'action.hover' }
                      }}
                    >
                      <CloseIcon sx={{ fontSize: 14 }} />
                    </Box>
                  </Box>
                } 
              />
            ))}
          </Tabs>
          <Tooltip title="Summarize & compress conversation">
            <span>
              <IconButton 
                size="small" 
                onClick={handleSummarize} 
                disabled={summarizing || loading || (tabs[currentTab]?.messages.length || 0) < 4}
                sx={{ mr: 0.5, color: estimatedTokens > 100000 ? 'warning.main' : 'text.secondary' }}
              >
                {summarizing ? <CircularProgress size={16} /> : <CompressIcon sx={{ fontSize: 18 }} />}
              </IconButton>
            </span>
          </Tooltip>
          <Tooltip title="Clear current chat">
            <IconButton size="small" onClick={handleClearChat} sx={{ mr: 0.5 }}><RefreshIcon sx={{ fontSize: 18 }} /></IconButton>
          </Tooltip>
          <Tooltip title="New chat tab">
            <IconButton size="small" onClick={handleNewTab} sx={{ mr: 1 }}><AddIcon sx={{ fontSize: 18 }} /></IconButton>
          </Tooltip>
        </Box>

        <Box sx={{ flex: 1, overflow: 'auto', p: 2 }}>
          {/* Context window warning banner */}
          {estimatedTokens > 100000 && tabs[currentTab]?.messages.length > 0 && (
            <Box sx={{
              p: 1.5, mb: 2, borderRadius: 2,
              bgcolor: estimatedTokens > 150000 
                ? alpha(theme.palette.error.main, 0.08) 
                : alpha(theme.palette.warning.main, 0.08),
              border: 1,
              borderColor: estimatedTokens > 150000 ? 'error.main' : 'warning.main',
              display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap'
            }}>
              <WarningIcon sx={{ fontSize: 18, color: estimatedTokens > 150000 ? 'error.main' : 'warning.main' }} />
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Typography variant="caption" sx={{ fontWeight: 600, color: estimatedTokens > 150000 ? 'error.main' : 'warning.main' }}>
                  {estimatedTokens > 150000 
                    ? 'Context nearly full - older messages will be auto-summarized on next send' 
                    : 'Long conversation - summarize now to keep things fast'}
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                  ~{Math.round(estimatedTokens / 1000)}k / 200k tokens used ({tabs[currentTab]?.messages.length} messages)
                </Typography>
              </Box>
              <Button
                size="small"
                variant="outlined"
                startIcon={summarizing ? <CircularProgress size={12} /> : <CompressIcon sx={{ fontSize: 14 }} />}
                onClick={handleSummarize}
                disabled={summarizing || loading || tabs[currentTab]?.messages.length < 4}
                sx={{ 
                  textTransform: 'none', fontSize: '0.7rem', py: 0.25, px: 1,
                  borderColor: estimatedTokens > 150000 ? 'error.main' : 'warning.main',
                  color: estimatedTokens > 150000 ? 'error.main' : 'warning.main',
                  '&:hover': {
                    borderColor: estimatedTokens > 150000 ? 'error.dark' : 'warning.dark',
                    bgcolor: estimatedTokens > 150000 
                      ? alpha(theme.palette.error.main, 0.08)
                      : alpha(theme.palette.warning.main, 0.08),
                  }
                }}
              >
                {summarizing ? 'Summarizing...' : 'Summarize & Continue'}
              </Button>
            </Box>
          )}
          
          {tabs[currentTab]?.messages.length === 0 && (
            <Box sx={{ textAlign: 'center', mt: 4 }}>
              <Typography variant="body2" color="text.secondary">Start a conversation</Typography>
              {selectedAgent && <Chip size="small" label={agents.find(a => a.id === selectedAgent)?.name} sx={{ mt: 1 }} />}
            </Box>
          )}
          {tabs[currentTab]?.messages.map((msg, i) => (
            <Box key={i} sx={{
                p: 1.5, mb: 1.5, borderRadius: 2,
                bgcolor: msg.role === 'user' ? alpha(theme.palette.primary.main, 0.1) : 'background.paper',
                ml: msg.role === 'user' ? 4 : 0, mr: msg.role === 'user' ? 0 : 4,
                border: 1, borderColor: msg.role === 'user' ? alpha(theme.palette.primary.main, 0.2) : 'divider',
              }}>
                <Typography variant="caption" sx={{ fontWeight: 600, color: msg.role === 'user' ? 'primary.main' : 'text.secondary', mb: 0.5, display: 'block' }}>
                  {msg.role === 'user' ? 'You' : 'DeepTempo'}
                </Typography>
                {renderContent(msg.content, i)}
              </Box>
            ))}
          
          {/* Show streaming thinking in real-time */}
          {isThinking && streamingThinking && (
            <Box sx={{
              p: 1.5, mb: 1.5, borderRadius: 2,
              bgcolor: 'background.paper',
              mr: 4,
              border: 1, borderColor: 'divider',
            }}>
              <Typography variant="caption" sx={{ fontWeight: 600, color: 'text.secondary', mb: 0.5, display: 'block' }}>
                DeepTempo
              </Typography>
              <Box sx={{ 
                p: 1.5, 
                borderRadius: 1, 
                bgcolor: alpha(theme.palette.info.main, 0.05),
                borderLeft: 2,
                borderColor: 'info.main',
                mb: 0.5,
              }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mb: 0.5 }}>
                  <Typography variant="caption" sx={{ fontWeight: 600, color: 'info.main', fontSize: '0.7rem' }}>
                    💭 THINKING...
                  </Typography>
                  <CircularProgress size={10} sx={{ ml: 1 }} />
                </Box>
                <Typography 
                  variant="body2" 
                  sx={{ 
                    whiteSpace: 'pre-wrap', 
                    fontStyle: 'italic', 
                    color: 'text.secondary',
                    fontSize: '0.85rem',
                    lineHeight: 1.5,
                    opacity: 0.9
                  }}
                >
                  {streamingThinking}
                </Typography>
              </Box>
            </Box>
          )}
          
          {/* Show streaming text in real-time */}
          {loading && streamingText && (
            <Box sx={{
              p: 1.5, mb: 1.5, borderRadius: 2,
              bgcolor: 'background.paper',
              mr: 4,
              border: 1, borderColor: 'divider',
            }}>
              <Typography variant="caption" sx={{ fontWeight: 600, color: 'text.secondary', mb: 0.5, display: 'block' }}>
                DeepTempo
              </Typography>
              <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>{streamingText}</Typography>
            </Box>
          )}
          
          {loading && !streamingText && !isThinking && <Box display="flex" justifyContent="center" my={2}><CircularProgress size={20} /></Box>}
          <div ref={messagesEndRef} />
        </Box>

        {attachedFiles.length > 0 && (
          <Box sx={{ px: 2, pb: 1 }}>
            <List dense>
              {attachedFiles.map((f, i) => (
                <ListItem key={i} sx={{ bgcolor: 'background.paper', borderRadius: 1, mb: 0.5, py: 0.5 }}>
                  <ImageIcon sx={{ fontSize: 16, mr: 1 }} />
                  <ListItemText primary={f.name} primaryTypographyProps={{ variant: 'caption' }} />
                  <ListItemSecondaryAction><IconButton size="small" onClick={() => setAttachedFiles(prev => prev.filter((_, j) => j !== i))}><DeleteIcon sx={{ fontSize: 16 }} /></IconButton></ListItemSecondaryAction>
                </ListItem>
              ))}
            </List>
          </Box>
        )}

        <Box sx={{ p: 2, borderTop: 1, borderColor: 'divider', bgcolor: 'background.paper' }}>
          <Box sx={{ display: 'flex', gap: 1, mb: 1, alignItems: 'center' }}>
            <input type="file" ref={fileInputRef} onChange={handleFileSelect} accept="image/*,.txt,.json,.csv,.md" multiple style={{ display: 'none' }} />
            <IconButton size="small" onClick={() => fileInputRef.current?.click()} disabled={loading}><AttachFileIcon sx={{ fontSize: 18 }} /></IconButton>
            <FormControl size="small" sx={{ minWidth: 140 }}>
              <Select 
                value={agents.find(a => a.id === selectedAgent) ? selectedAgent : ''} 
                onChange={(e) => setSelectedAgent(e.target.value)} 
                displayEmpty
              >
                <MenuItem value=""><em>No Agent</em></MenuItem>
                {agents.map(a => <MenuItem key={a.id} value={a.id}>{a.icon} {a.name}</MenuItem>)}
              </Select>
            </FormControl>
            <Tooltip title="View agents"><IconButton size="small" onClick={() => setAgentInfoDialogOpen(true)}><InfoIcon sx={{ fontSize: 18 }} /></IconButton></Tooltip>
            <Box sx={{ flex: 1 }} />
            <Tooltip title="Generate PDF report">
              <IconButton size="small" disabled={!tabs[currentTab]?.messages.length || loading} onClick={async () => {
                try {
                  setLoading(true)
                  const res = await claudeApi.generateChatReport({ tab_title: tabs[currentTab].title, messages: tabs[currentTab].messages })
                  alert(`Report saved: ${res.data.filename}`)
                } catch { /* ignore */ } finally { setLoading(false) }
              }}><PdfIcon sx={{ fontSize: 18 }} /></IconButton>
            </Tooltip>
          </Box>
          <Box sx={{ display: 'flex', gap: 1 }}>
            <TextField fullWidth multiline maxRows={3} value={input} onChange={(e) => setInput(e.target.value)} onKeyPress={handleKeyPress} placeholder="Message..." disabled={loading} size="small" />
            <Button variant="contained" onClick={handleSend} disabled={(!input.trim() && !attachedFiles.length) || loading} sx={{ minWidth: 44 }}><SendIcon sx={{ fontSize: 18 }} /></Button>
          </Box>
        </Box>
      </Box>

      <Dialog open={agentInfoDialogOpen} onClose={() => setAgentInfoDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>SOC Agents</DialogTitle>
        <DialogContent dividers>
          {agents.map(a => (
            <Box key={a.id} sx={{ mb: 2, p: 1.5, borderRadius: 2, bgcolor: 'background.default', borderLeft: 3, borderColor: a.color || 'primary.main' }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <Typography sx={{ fontSize: '1.25rem' }}>{a.icon}</Typography>
                <Box>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>{a.name}</Typography>
                  {a.specialization && <Chip label={a.specialization} size="small" sx={{ height: 18, fontSize: '0.65rem' }} />}
                </Box>
              </Box>
              <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: 'block' }}>{a.description}</Typography>
            </Box>
          ))}
        </DialogContent>
        <DialogActions><Button onClick={() => setAgentInfoDialogOpen(false)}>Close</Button></DialogActions>
      </Dialog>

      {/* GH #79 — Reasoning trace drawer */}
      <Dialog open={traceOpen} onClose={() => setTraceOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <ReasoningIcon fontSize="small" />
          Reasoning Trace
          {sessionSummary && (
            <Chip
              size="small"
              label={`${sessionSummary.total_interactions} calls · $${sessionSummary.total_cost_usd.toFixed(4)}`}
              sx={{ ml: 1 }}
            />
          )}
        </DialogTitle>
        <DialogContent dividers sx={{ display: 'flex', gap: 2, p: 0, height: '70vh' }}>
          <Box sx={{ width: 260, borderRight: 1, borderColor: 'divider', overflow: 'auto' }}>
            {traceLoading ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', p: 3 }}><CircularProgress size={18} /></Box>
            ) : traceInteractions.length === 0 ? (
              <Box sx={{ p: 2 }}><Typography variant="caption" color="text.secondary">No reasoning traces yet.</Typography></Box>
            ) : (
              <List dense>
                {traceInteractions.map((it) => (
                  <ListItem
                    key={it.interaction_id}
                    button
                    selected={traceSelected?.interaction_id === it.interaction_id}
                    onClick={() => loadTraceInteraction(it.interaction_id)}
                    sx={{ py: 0.5, alignItems: 'flex-start' }}
                  >
                    <ListItemText
                      primary={
                        <Box sx={{ display: 'flex', gap: 0.5, alignItems: 'center' }}>
                          <Typography variant="caption" sx={{ fontFamily: 'monospace' }}>
                            {it.created_at ? new Date(it.created_at).toLocaleTimeString() : ''}
                          </Typography>
                          {it.has_thinking && <Chip label="💭" size="small" sx={{ height: 16, fontSize: '0.65rem' }} />}
                          {it.has_tools && <Chip label="🔧" size="small" sx={{ height: 16, fontSize: '0.65rem' }} />}
                        </Box>
                      }
                      secondary={
                        <Typography variant="caption" color="text.secondary">
                          {it.agent_id || 'chat'} · {it.input_tokens}/{it.output_tokens} tok · ${(it.cost_usd ?? 0).toFixed(4)}
                        </Typography>
                      }
                    />
                  </ListItem>
                ))}
              </List>
            )}
          </Box>
          <Box sx={{ flex: 1, overflow: 'auto', p: 2 }}>
            {!traceSelected ? (
              <Typography variant="body2" color="text.secondary">
                Select an interaction to view full reasoning, tool calls, and response.
              </Typography>
            ) : (
              <Box>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                  {traceSelected.model} · stop={traceSelected.stop_reason || '—'} · {traceSelected.duration_ms}ms · ${(traceSelected.cost_usd ?? 0).toFixed(4)}
                </Typography>

                {traceSelected.thinking_content && (
                  <Box sx={{ mb: 2, p: 1.5, bgcolor: alpha(theme.palette.info.main, 0.05), borderLeft: 2, borderColor: 'info.main' }}>
                    <Typography variant="caption" sx={{ fontWeight: 600, color: 'info.main', display: 'block', mb: 0.5 }}>
                      💭 Thinking ({traceSelected.thinking_content.length} chars)
                    </Typography>
                    <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '0.75rem' }}>
                      {traceSelected.thinking_content}
                    </Typography>
                  </Box>
                )}

                {traceSelected.response_content && (
                  <Box sx={{ mb: 2, p: 1.5, bgcolor: 'background.paper', border: 1, borderColor: 'divider', borderRadius: 1 }}>
                    <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.5 }}>Response</Typography>
                    <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', fontSize: '0.85rem' }}>
                      {traceSelected.response_content}
                    </Typography>
                  </Box>
                )}

                {Array.isArray(traceSelected.tool_calls) && traceSelected.tool_calls.length > 0 && (
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.5 }}>Tool Calls</Typography>
                    {traceSelected.tool_calls.map((tc: any, i: number) => (
                      <Box key={i} sx={{ mb: 1, p: 1, bgcolor: alpha(theme.palette.warning.main, 0.05), borderLeft: 2, borderColor: 'warning.main' }}>
                        <Typography variant="caption" sx={{ fontWeight: 600, fontFamily: 'monospace' }}>🔧 {tc.name}</Typography>
                        <Typography variant="body2" component="pre" sx={{ fontFamily: 'monospace', fontSize: '0.7rem', whiteSpace: 'pre-wrap', m: 0, mt: 0.5 }}>
                          {JSON.stringify(tc.input, null, 2)}
                        </Typography>
                      </Box>
                    ))}
                  </Box>
                )}

                {Array.isArray(traceSelected.tool_results) && traceSelected.tool_results.length > 0 && (
                  <Box sx={{ mb: 2 }}>
                    <Typography variant="caption" sx={{ fontWeight: 600, display: 'block', mb: 0.5 }}>Tool Results (input to this call)</Typography>
                    {traceSelected.tool_results.map((tr: any, i: number) => (
                      <Box key={i} sx={{ mb: 1, p: 1, bgcolor: alpha(theme.palette.success.main, 0.05), borderLeft: 2, borderColor: tr.is_error ? 'error.main' : 'success.main' }}>
                        <Typography variant="caption" sx={{ fontWeight: 600, fontFamily: 'monospace' }}>
                          {tr.is_error ? '❌' : '✅'} {tr.tool_use_id}
                        </Typography>
                        <Typography variant="body2" component="pre" sx={{ fontFamily: 'monospace', fontSize: '0.7rem', whiteSpace: 'pre-wrap', m: 0, mt: 0.5 }}>
                          {typeof tr.content === 'string' ? tr.content : JSON.stringify(tr.content, null, 2)}
                        </Typography>
                      </Box>
                    ))}
                  </Box>
                )}
              </Box>
            )}
          </Box>
        </DialogContent>
        <DialogActions><Button onClick={() => setTraceOpen(false)}>Close</Button></DialogActions>
      </Dialog>
    </Drawer>
  )
}
