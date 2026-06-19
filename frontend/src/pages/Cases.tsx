import { useState } from 'react'
import {
  Box,
  Typography,
  Button,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Select,
  MenuItem,
  FormControl,
  Snackbar,
  Alert,
  IconButton,
  Collapse,
} from '@mui/material'
import { Add as AddIcon, Refresh as RefreshIcon, FilterList as FilterIcon } from '@mui/icons-material'
import CasesTable from '../components/cases/CasesTable'
import CaseSearch from '../components/cases/CaseSearch'
import { casesApi } from '../services/api'
import { SearchInput } from '../components/ui'

export default function Cases() {
  const [filters, setFilters] = useState({ status: '', priority: '' })
  const [searchQuery, setSearchQuery] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const [createDialogOpen, setCreateDialogOpen] = useState(false)
  const [newCase, setNewCase] = useState({ title: '', description: '', priority: 'medium' })
  const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: 'success' | 'error' }>({
    open: false, message: '', severity: 'success',
  })
  const [showFilters, setShowFilters] = useState(false)
  const [showAdvancedSearch, setShowAdvancedSearch] = useState(false)

  const handleRefresh = () => setRefreshKey(prev => prev + 1)

  const handleCreateCase = async () => {
    if (!newCase.title.trim()) {
      setSnackbar({ open: true, message: 'Please enter a case title', severity: 'error' })
      return
    }
    try {
      await casesApi.create({ ...newCase, finding_ids: [] })
      setCreateDialogOpen(false)
      setNewCase({ title: '', description: '', priority: 'medium' })
      setSnackbar({ open: true, message: 'Case created successfully', severity: 'success' })
      handleRefresh()
    } catch (error) {
      console.error('Failed to create case:', error)
      setSnackbar({ open: true, message: 'Failed to create case', severity: 'error' })
    }
  }

  return (
    <Box>
      <Box display="flex" justifyContent="space-between" alignItems="flex-start"  sx={{ mb: 3, pr: 10 }}>
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 700, mb: 0.5 }}>
            Cases
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Manage investigation cases
          </Typography>
        </Box>
        <Box display="flex" alignItems="center" gap={1}>
          <Button
            size="small"
            variant={showAdvancedSearch ? 'contained' : 'outlined'}
            onClick={() => setShowAdvancedSearch(!showAdvancedSearch)}
          >
            Advanced Search
          </Button>
          <IconButton
            size="small"
            onClick={handleRefresh}
            sx={{ color: 'text.secondary' }}
          >
            <RefreshIcon sx={{ fontSize: 20 }} />
          </IconButton>
          <Button
            size="small"
            variant="contained"
            startIcon={<AddIcon />}
            onClick={() => setCreateDialogOpen(true)}
          >
            New Case
          </Button>
        </Box>
      </Box>

      {showAdvancedSearch && (
        <CaseSearch onResultsChange={() => {}} />
      )}

      <Box sx={{ bgcolor: 'background.paper', borderRadius: 3, border: 1, borderColor: 'divider' }}>
        <Box sx={{ p: 2, borderBottom: 1, borderColor: 'divider', display: 'flex', gap: 2, alignItems: 'center' }}>
          <Box sx={{ flex: 1, maxWidth: 400 }}>
            <SearchInput
              value={searchQuery}
              onChange={setSearchQuery}
              placeholder="Search cases..."
            />
          </Box>
          <IconButton size="small" onClick={() => setShowFilters(!showFilters)}>
            <FilterIcon sx={{ fontSize: 20, color: showFilters ? 'primary.main' : 'text.secondary' }} />
          </IconButton>
        </Box>

        <Collapse in={showFilters}>
          <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: 'divider', display: 'flex', gap: 2 }}>
            <FormControl size="small" sx={{ minWidth: 120 }}>
              <Select
                value={filters.status}
                onChange={(e) => setFilters({ ...filters, status: e.target.value })}
                displayEmpty
              >
                <MenuItem value="">All Status</MenuItem>
                <MenuItem value="open">Open</MenuItem>
                <MenuItem value="in-progress">In Progress</MenuItem>
                <MenuItem value="resolved">Resolved</MenuItem>
                <MenuItem value="closed">Closed</MenuItem>
              </Select>
            </FormControl>
            <FormControl size="small" sx={{ minWidth: 120 }}>
              <Select
                value={filters.priority}
                onChange={(e) => setFilters({ ...filters, priority: e.target.value })}
                displayEmpty
              >
                <MenuItem value="">All Priority</MenuItem>
                <MenuItem value="critical">Critical</MenuItem>
                <MenuItem value="high">High</MenuItem>
                <MenuItem value="medium">Medium</MenuItem>
                <MenuItem value="low">Low</MenuItem>
              </Select>
            </FormControl>
          </Box>
        </Collapse>

        <Box sx={{ p: 2 }}>
          <CasesTable filters={filters} searchQuery={searchQuery} refreshKey={refreshKey} />
        </Box>
      </Box>

      <Dialog open={createDialogOpen} onClose={() => setCreateDialogOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ pb: 1 }}>Create New Case</DialogTitle>
        <DialogContent sx={{ pt: 2 }}>
          <TextField
            autoFocus
            label="Title"
            fullWidth
            value={newCase.title}
            onChange={(e) => setNewCase({ ...newCase, title: e.target.value })}
            sx={{ mb: 2 }}
          />
          <TextField
            label="Description"
            fullWidth
            multiline
            rows={3}
            value={newCase.description}
            onChange={(e) => setNewCase({ ...newCase, description: e.target.value })}
            sx={{ mb: 2 }}
          />
          <FormControl fullWidth>
            <Select
              value={newCase.priority}
              onChange={(e) => setNewCase({ ...newCase, priority: e.target.value })}
              displayEmpty
            >
              <MenuItem value="low">Low Priority</MenuItem>
              <MenuItem value="medium">Medium Priority</MenuItem>
              <MenuItem value="high">High Priority</MenuItem>
              <MenuItem value="critical">Critical Priority</MenuItem>
            </Select>
          </FormControl>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setCreateDialogOpen(false)}>Cancel</Button>
          <Button onClick={handleCreateCase} variant="contained">
            Create
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={snackbar.open}
        autoHideDuration={3000}
        onClose={() => setSnackbar({ ...snackbar, open: false })}
      >
        <Alert onClose={() => setSnackbar({ ...snackbar, open: false })} severity={snackbar.severity} variant="filled">
          {snackbar.message}
        </Alert>
      </Snackbar>
    </Box>
  )
}
