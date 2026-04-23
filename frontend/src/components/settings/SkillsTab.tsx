import { useEffect, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControl,
  Grid,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Switch,
  Tooltip,
  Typography,
} from '@mui/material'
import {
  Add as AddIcon,
  AutoAwesome as SparkIcon,
  Delete as DeleteIcon,
  Refresh as RefreshIcon,
  UploadFile as UploadFileIcon,
} from '@mui/icons-material'

import {
  Skill,
  SkillCategory,
  SKILL_CATEGORIES,
  skillsApi,
} from '../../services/skillsApi'
import SkillBuilder from './SkillBuilder'

const CATEGORY_COLOR: Record<SkillCategory, 'primary' | 'info' | 'warning' | 'success' | 'default'> = {
  detection: 'warning',
  enrichment: 'info',
  response: 'primary',
  reporting: 'success',
  custom: 'default',
}

export default function SkillsTab() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [importInfo, setImportInfo] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)
  const [categoryFilter, setCategoryFilter] = useState<SkillCategory | ''>('')
  const [builderOpen, setBuilderOpen] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState<Skill | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const params: { category?: SkillCategory } = {}
      if (categoryFilter) params.category = categoryFilter
      const list = await skillsApi.list(params)
      setSkills(list)
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to load skills')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [categoryFilter])

  const handleToggleActive = async (skill: Skill) => {
    try {
      const updated = await skillsApi.update(skill.skill_id, { is_active: !skill.is_active })
      setSkills((prev) =>
        prev.map((s) => (s.skill_id === updated.skill_id ? updated : s))
      )
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to update skill')
    }
  }

  const handleDelete = async () => {
    if (!confirmDelete) return
    try {
      await skillsApi.remove(confirmDelete.skill_id)
      setSkills((prev) => prev.filter((s) => s.skill_id !== confirmDelete.skill_id))
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to delete skill')
    } finally {
      setConfirmDelete(null)
    }
  }

  const handleImportClick = () => {
    fileInputRef.current?.click()
  }

  const handleImportChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    // Reset the input so the same file can be re-picked after an error.
    e.target.value = ''
    if (!file) return
    setImporting(true)
    setError(null)
    setImportInfo(null)
    try {
      const result = await skillsApi.importZip(file)
      const verb = result.replaced ? 'updated' : 'imported'
      setImportInfo(`Successfully ${verb} "${result.name}" (v${result.version}).`)
      await load()
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      const message =
        typeof detail === 'string'
          ? detail
          : detail?.message || err.message || 'Failed to import skill zip'
      const rejected: string[] | undefined = detail?.details?.rejected_paths
      setError(
        rejected && rejected.length > 0
          ? `${message} (offending paths: ${rejected.join(', ')})`
          : message
      )
    } finally {
      setImporting(false)
    }
  }

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2, flexWrap: 'wrap' }}>
        <Box>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            Skills
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Reusable, parameterized capabilities agents and workflows can invoke.
          </Typography>
        </Box>
        <Box sx={{ flex: 1 }} />
        <FormControl size="small" sx={{ minWidth: 180 }}>
          <InputLabel>Category</InputLabel>
          <Select
            label="Category"
            value={categoryFilter}
            onChange={(e) => setCategoryFilter(e.target.value as SkillCategory | '')}
          >
            <MenuItem value="">All</MenuItem>
            {SKILL_CATEGORIES.map((c) => (
              <MenuItem key={c} value={c}>
                {c}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <Button
          size="small"
          variant="text"
          startIcon={<RefreshIcon />}
          onClick={load}
          disabled={loading}
        >
          Refresh
        </Button>
        <Button
          variant="outlined"
          startIcon={<UploadFileIcon />}
          onClick={handleImportClick}
          disabled={importing}
        >
          {importing ? 'Importing…' : 'Import Zip'}
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".zip,application/zip"
          hidden
          onChange={handleImportChange}
        />
        <Button
          variant="contained"
          startIcon={<SparkIcon />}
          onClick={() => setBuilderOpen(true)}
        >
          Build Skill
        </Button>
      </Box>

      {importInfo && (
        <Alert severity="success" sx={{ mb: 2 }} onClose={() => setImportInfo(null)}>
          {importInfo}
        </Alert>
      )}

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {loading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}>
          <CircularProgress size={28} />
        </Box>
      ) : skills.length === 0 ? (
        <Card variant="outlined">
          <CardContent sx={{ textAlign: 'center', py: 4 }}>
            <Typography variant="body1" gutterBottom>
              No skills yet.
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Click <strong>Build Skill</strong> to have Claude draft your first one.
            </Typography>
            <Button
              variant="outlined"
              startIcon={<AddIcon />}
              onClick={() => setBuilderOpen(true)}
            >
              Build your first skill
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Grid container spacing={2}>
          {skills.map((skill) => (
            <Grid item xs={12} md={6} key={skill.skill_id}>
              <Card variant="outlined">
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, mb: 1 }}>
                    <Box sx={{ flex: 1, minWidth: 0 }}>
                      <Typography variant="subtitle1" sx={{ fontWeight: 600 }} noWrap>
                        {skill.name}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        {skill.skill_id} · v{skill.version}
                      </Typography>
                    </Box>
                    <Chip
                      label={skill.category}
                      size="small"
                      color={CATEGORY_COLOR[skill.category as SkillCategory] || 'default'}
                    />
                  </Box>

                  {skill.description && (
                    <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                      {skill.description}
                    </Typography>
                  )}

                  {skill.required_tools?.length > 0 && (
                    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mb: 1 }}>
                      {skill.required_tools.map((t) => (
                        <Chip key={t} label={t} size="small" variant="outlined" />
                      ))}
                    </Box>
                  )}

                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1 }}>
                    <Tooltip title={skill.is_active ? 'Active' : 'Inactive'}>
                      <Switch
                        size="small"
                        checked={skill.is_active}
                        onChange={() => handleToggleActive(skill)}
                      />
                    </Tooltip>
                    <Typography variant="caption" color="text.secondary">
                      {skill.is_active ? 'Active' : 'Inactive'}
                    </Typography>
                    <Box sx={{ flex: 1 }} />
                    <IconButton
                      size="small"
                      color="error"
                      onClick={() => setConfirmDelete(skill)}
                      aria-label={`Delete ${skill.name}`}
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Box>
                </CardContent>
              </Card>
            </Grid>
          ))}
        </Grid>
      )}

      <SkillBuilder
        open={builderOpen}
        onClose={() => setBuilderOpen(false)}
        onSaved={() => {
          setBuilderOpen(false)
          load()
        }}
      />

      <Dialog open={Boolean(confirmDelete)} onClose={() => setConfirmDelete(null)}>
        <DialogTitle>Delete skill?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            {confirmDelete
              ? `This will permanently remove the skill "${confirmDelete.name}".`
              : ''}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDelete(null)}>Cancel</Button>
          <Button onClick={handleDelete} color="error" variant="contained">
            Delete
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  )
}
