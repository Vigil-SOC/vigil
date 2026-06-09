import { useState } from 'react'
import { basePath } from '../../config/basePath'
import {
  Box,
  Button,
  TextField,
  Typography,
  CircularProgress,
  Alert,
  Stepper,
  Step,
  StepLabel,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Card,
  CardContent,
  Chip,
  IconButton,
  Divider,
} from '@mui/material'
import {
  CloudUpload as UploadIcon,
  Code as CodeIcon,
  Check as CheckIcon,
  Error as ErrorIcon,
  Info as InfoIcon,
  Visibility as PreviewIcon,
  Save as SaveIcon,
  Close as CloseIcon,
} from '@mui/icons-material'
import { INTEGRATION_CATEGORIES } from '../../config/integrations'

interface CustomIntegrationBuilderProps {
  onClose: () => void
  onSave: (integrationId: string) => void
}

interface GeneratedIntegration {
  integration_id: string
  integration_name: string
  metadata: any
  server_code: string
}

const steps = ['Provide Documentation', 'Review & Edit', 'Test & Save']

export default function CustomIntegrationBuilder({ onClose, onSave }: CustomIntegrationBuilderProps) {
  const [activeStep, setActiveStep] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // Step 1: Documentation input
  const [documentation, setDocumentation] = useState('')
  const [integrationName, setIntegrationName] = useState('')
  const [category, setCategory] = useState('Custom')
  const [uploadedFile, setUploadedFile] = useState<File | null>(null)

  // Step 2: Generated integration
  const [generatedIntegration, setGeneratedIntegration] = useState<GeneratedIntegration | null>(null)
  const [editedMetadata, setEditedMetadata] = useState<any>(null)
  const [editedServerCode, setEditedServerCode] = useState<string>('')

  // Interactive mode
  const [needsClarification, setNeedsClarification] = useState(false)
  const [conversationHistory, setConversationHistory] = useState<any[]>([])
  const [claudeQuestion, setClaudeQuestion] = useState<string>('')
  const [userAnswer, setUserAnswer] = useState<string>('')

  // Step 3: Validation results
  const [validationResults, setValidationResults] = useState<any>(null)
  const [showCodePreview, setShowCodePreview] = useState(false)

  const handleFileUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (file) {
      setUploadedFile(file)
      // Read file content
      const reader = new FileReader()
      reader.onload = (e) => {
        const content = e.target?.result as string
        setDocumentation(content)
      }
      reader.readAsText(file)
    }
  }

  const handleGenerate = async (userResponse?: string) => {
    if (!documentation.trim() && !userResponse) {
      setError('Please provide API documentation')
      return
    }

    setLoading(true)
    setError(null)

    try {
      const response = await fetch(`${basePath}/api/custom-integrations/generate`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          documentation: documentation,
          integration_name: integrationName || null,
          category: category,
          conversation_history: conversationHistory.length > 0 ? conversationHistory : null,
          user_response: userResponse || null,
        }),
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to generate integration')
      }

      const result = await response.json()

      if (!result.success) {
        throw new Error(result.error || 'Failed to generate integration')
      }

      // Check if Claude is asking questions
      if (result.needs_clarification) {
        setNeedsClarification(true)
        setClaudeQuestion(result.message)
        setConversationHistory(result.conversation_history || [])
        setUserAnswer('')
      } else {
        // Integration generated successfully
        setGeneratedIntegration(result)
        setEditedMetadata(result.metadata)
        setEditedServerCode(result.server_code)
        setSuccess('Integration generated successfully!')
        setNeedsClarification(false)
        setActiveStep(1)
      }
    } catch (err: any) {
      setError(err.message || 'Failed to generate integration')
    } finally {
      setLoading(false)
    }
  }

  const handleAnswerQuestion = () => {
    if (!userAnswer.trim()) {
      setError('Please provide an answer')
      return
    }
    handleGenerate(userAnswer)
  }

  const handleValidate = async () => {
    if (!generatedIntegration) return

    setLoading(true)
    setError(null)

    try {
      // First save the integration temporarily
      const saveResponse = await fetch(`${basePath}/api/custom-integrations/save`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          integration_id: generatedIntegration.integration_id,
          metadata: editedMetadata,
          server_code: editedServerCode,
        }),
      })

      if (!saveResponse.ok) {
        throw new Error('Failed to save integration for validation')
      }

      // Now validate it
      const validateResponse = await fetch(
        `${basePath}/api/custom-integrations/${generatedIntegration.integration_id}/validate`,
        {
          method: 'POST',
          credentials: 'include',
        }
      )

      if (!validateResponse.ok) {
        throw new Error('Failed to validate integration')
      }

      const result = await validateResponse.json()
      setValidationResults(result)
      setActiveStep(2)
    } catch (err: any) {
      setError(err.message || 'Failed to validate integration')
    } finally {
      setLoading(false)
    }
  }

  const handleFinalSave = async () => {
    if (!generatedIntegration) return

    setLoading(true)
    setError(null)

    try {
      const response = await fetch(`${basePath}/api/custom-integrations/save`, {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          integration_id: generatedIntegration.integration_id,
          metadata: editedMetadata,
          server_code: editedServerCode,
        }),
      })

      if (!response.ok) {
        throw new Error('Failed to save integration')
      }

      await response.json()
      setSuccess('Custom integration saved successfully!')
      
      // Call the onSave callback after a short delay
      setTimeout(() => {
        onSave(generatedIntegration.integration_id)
      }, 1500)
    } catch (err: any) {
      setError(err.message || 'Failed to save integration')
    } finally {
      setLoading(false)
    }
  }

  const handleNext = () => {
    if (activeStep === 0) {
      if (needsClarification) {
        handleAnswerQuestion()
      } else {
        handleGenerate()
      }
    } else if (activeStep === 1) {
      handleValidate()
    } else if (activeStep === 2) {
      handleFinalSave()
    }
  }

  const handleBack = () => {
    setActiveStep((prev) => prev - 1)
    setError(null)
    setSuccess(null)
  }

  const renderStep = () => {
    switch (activeStep) {
      case 0:
        return (
          <Box>
            {!needsClarification ? (
              <>
                <Typography variant="body1" gutterBottom color="text.secondary" sx={{ mb: 3 }}>
                  Paste API documentation or upload a file. Our AI will analyze it and generate a
                  complete integration including MCP server code and configuration.
                </Typography>

            <FormControl fullWidth sx={{ mb: 3 }}>
              <InputLabel>Integration Category</InputLabel>
              <Select
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                label="Integration Category"
              >
                {INTEGRATION_CATEGORIES.map((cat) => (
                  <MenuItem key={cat} value={cat}>
                    {cat}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>

            <TextField
              fullWidth
              label="Integration Name (optional)"
              value={integrationName}
              onChange={(e) => setIntegrationName(e.target.value)}
              placeholder="e.g., My Security Tool"
              sx={{ mb: 3 }}
              helperText="Leave blank to auto-generate from documentation"
            />

            <Box sx={{ mb: 2 }}>
              <Button
                variant="outlined"
                component="label"
                startIcon={<UploadIcon />}
                fullWidth
              >
                Upload Documentation File
                <input
                  type="file"
                  hidden
                  accept=".txt,.md,.pdf,.doc,.docx"
                  onChange={handleFileUpload}
                />
              </Button>
              {uploadedFile && (
                <Chip
                  label={uploadedFile.name}
                  onDelete={() => {
                    setUploadedFile(null)
                    setDocumentation('')
                  }}
                  sx={{ mt: 1 }}
                />
              )}
            </Box>

            <Divider sx={{ my: 2 }}>
              <Typography variant="caption" color="text.secondary">
                OR
              </Typography>
            </Divider>

                <TextField
                  fullWidth
                  multiline
                  rows={12}
                  label="API Documentation"
                  value={documentation}
                  onChange={(e) => setDocumentation(e.target.value)}
                  placeholder="Paste API documentation here...&#10;&#10;Include:&#10;- API endpoints and methods&#10;- Authentication details&#10;- Request/response examples&#10;- Parameter descriptions"
                  sx={{ fontFamily: 'monospace' }}
                />
              </>
            ) : (
              <>
                <Alert severity="info" icon={<InfoIcon />} sx={{ mb: 3 }}>
                  Claude needs more information to generate a complete integration. Please answer
                  the questions below.
                </Alert>

                <Card sx={{ mb: 3, bgcolor: 'background.default' }}>
                  <CardContent>
                    <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2 }}>
                      <Box
                        sx={{
                          width: 40,
                          height: 40,
                          borderRadius: '50%',
                          bgcolor: 'primary.main',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          flexShrink: 0,
                        }}
                      >
                        <Typography variant="h6" color="white">
                          AI
                        </Typography>
                      </Box>
                      <Box sx={{ flex: 1 }}>
                        <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap' }}>
                          {claudeQuestion}
                        </Typography>
                      </Box>
                    </Box>
                  </CardContent>
                </Card>

                <TextField
                  fullWidth
                  multiline
                  rows={6}
                  label="Your Answer"
                  value={userAnswer}
                  onChange={(e) => setUserAnswer(e.target.value)}
                  placeholder="Type your answer here...&#10;&#10;Be as specific as possible to help Claude generate the best integration."
                  sx={{ mb: 2 }}
                />

                <Button
                  variant="contained"
                  onClick={handleAnswerQuestion}
                  disabled={loading || !userAnswer.trim()}
                  startIcon={loading ? <CircularProgress size={20} /> : null}
                  fullWidth
                >
                  {loading ? 'Processing...' : 'Send Answer'}
                </Button>
              </>
            )}
          </Box>
        )

      case 1:
        return (
          <Box>
            {generatedIntegration && (
              <>
                <Alert severity="info" icon={<InfoIcon />} sx={{ mb: 3 }}>
                  Review the generated integration. You can edit the configuration and server code
                  before saving.
                </Alert>

                <Card sx={{ mb: 3 }}>
                  <CardContent>
                    <Typography variant="h6" gutterBottom>
                      Integration Details
                    </Typography>
                    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mt: 2 }}>
                      <Box>
                        <Typography variant="subtitle2" color="text.secondary">
                          Name
                        </Typography>
                        <Typography variant="body1">
                          {generatedIntegration.integration_name}
                        </Typography>
                      </Box>
                      <Box>
                        <Typography variant="subtitle2" color="text.secondary">
                          ID
                        </Typography>
                        <Typography variant="body1" sx={{ fontFamily: 'monospace' }}>
                          {generatedIntegration.integration_id}
                        </Typography>
                      </Box>
                      <Box>
                        <Typography variant="subtitle2" color="text.secondary">
                          Category
                        </Typography>
                        <Typography variant="body1">{editedMetadata?.category}</Typography>
                      </Box>
                      <Box>
                        <Typography variant="subtitle2" color="text.secondary">
                          Description
                        </Typography>
                        <Typography variant="body1">{editedMetadata?.description}</Typography>
                      </Box>
                      <Box>
                        <Typography variant="subtitle2" color="text.secondary">
                          Configuration Fields
                        </Typography>
                        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mt: 1 }}>
                          {editedMetadata?.fields?.map((field: any) => (
                            <Chip
                              key={field.name}
                              label={`${field.label} (${field.type})`}
                              size="small"
                              color={field.required ? 'primary' : 'default'}
                            />
                          ))}
                        </Box>
                      </Box>
                    </Box>
                  </CardContent>
                </Card>

                <Card>
                  <CardContent>
                    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                      <Typography variant="h6">
                        <CodeIcon sx={{ verticalAlign: 'middle', mr: 1 }} />
                        MCP Server Code
                      </Typography>
                      <Button
                        size="small"
                        startIcon={<PreviewIcon />}
                        onClick={() => setShowCodePreview(true)}
                      >
                        View Full Code
                      </Button>
                    </Box>
                    <Typography variant="body2" color="text.secondary">
                      {editedServerCode.split('\n').length} lines of Python code generated
                    </Typography>
                  </CardContent>
                </Card>
              </>
            )}
          </Box>
        )

      case 2:
        return (
          <Box>
            {validationResults && (
              <>
                <Alert
                  severity={validationResults.valid ? 'success' : 'warning'}
                  icon={validationResults.valid ? <CheckIcon /> : <ErrorIcon />}
                  sx={{ mb: 3 }}
                >
                  {validationResults.valid
                    ? 'Integration code is valid and ready to use!'
                    : 'There are some issues with the generated code. You may need to edit it manually.'}
                </Alert>

                <Card sx={{ mb: 3 }}>
                  <CardContent>
                    <Typography variant="h6" gutterBottom>
                      Validation Results
                    </Typography>
                    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, mt: 2 }}>
                      {validationResults.checks &&
                        Object.entries(validationResults.checks).map(([key, value]) => (
                          <Box key={key} sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                            {value ? (
                              <CheckIcon color="success" fontSize="small" />
                            ) : (
                              <ErrorIcon color="error" fontSize="small" />
                            )}
                            <Typography variant="body2">
                              {key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase())}
                            </Typography>
                          </Box>
                        ))}
                    </Box>
                    {validationResults.syntax_error && (
                      <Alert severity="error" sx={{ mt: 2 }}>
                        <Typography variant="subtitle2">Syntax Error:</Typography>
                        <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                          {validationResults.syntax_error}
                        </Typography>
                      </Alert>
                    )}
                  </CardContent>
                </Card>

                <Alert severity="info" icon={<InfoIcon />}>
                  After saving, you'll need to:
                  <ol style={{ marginTop: 8, marginBottom: 0 }}>
                    <li>Configure the integration in the Integrations tab</li>
                    <li>Enable it in your integration list</li>
                    <li>Restart MCP servers if needed</li>
                  </ol>
                </Alert>
              </>
            )}
          </Box>
        )

      default:
        return null
    }
  }

  return (
    <>
      <Dialog open fullWidth maxWidth="md" onClose={onClose}>
        <DialogTitle>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Typography variant="h6">AI-Powered Custom Integration Builder</Typography>
            <IconButton onClick={onClose} size="small">
              <CloseIcon />
            </IconButton>
          </Box>
        </DialogTitle>

        <DialogContent dividers>
          <Stepper activeStep={activeStep} sx={{ mb: 4 }}>
            {steps.map((label) => (
              <Step key={label}>
                <StepLabel>{label}</StepLabel>
              </Step>
            ))}
          </Stepper>

          {error && (
            <Alert severity="error" sx={{ mb: 3 }} onClose={() => setError(null)}>
              {error}
            </Alert>
          )}

          {success && (
            <Alert severity="success" sx={{ mb: 3 }} onClose={() => setSuccess(null)}>
              {success}
            </Alert>
          )}

          {renderStep()}
        </DialogContent>

        <DialogActions>
          <Button onClick={onClose} disabled={loading}>
            Cancel
          </Button>
          {activeStep > 0 && (
            <Button onClick={handleBack} disabled={loading}>
              Back
            </Button>
          )}
          <Button
            variant="contained"
            onClick={handleNext}
            disabled={
              loading ||
              (activeStep === 0 && !needsClarification && !documentation.trim()) ||
              (activeStep === 0 && needsClarification && !userAnswer.trim())
            }
            startIcon={
              loading ? (
                <CircularProgress size={20} />
              ) : activeStep === 2 ? (
                <SaveIcon />
              ) : null
            }
          >
            {loading
              ? 'Processing...'
              : activeStep === 2
              ? 'Save Integration'
              : activeStep === 1
              ? 'Validate'
              : needsClarification
              ? 'Send Answer'
              : 'Generate'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* Code Preview Dialog */}
      <Dialog
        open={showCodePreview}
        onClose={() => setShowCodePreview(false)}
        fullWidth
        maxWidth="lg"
      >
        <DialogTitle>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Typography variant="h6">MCP Server Code</Typography>
            <IconButton onClick={() => setShowCodePreview(false)} size="small">
              <CloseIcon />
            </IconButton>
          </Box>
        </DialogTitle>
        <DialogContent dividers>
          <TextField
            fullWidth
            multiline
            rows={25}
            value={editedServerCode}
            onChange={(e) => setEditedServerCode(e.target.value)}
            sx={{
              fontFamily: 'monospace',
              fontSize: '0.875rem',
              '& .MuiInputBase-input': {
                fontFamily: 'monospace',
              },
            }}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setShowCodePreview(false)}>Close</Button>
        </DialogActions>
      </Dialog>
    </>
  )
}

