import {
  Accordion,
  AccordionSummary,
  AccordionDetails,
  Box,
  Chip,
  Divider,
  Grid,
  Paper,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import {
  AccountTree as TopologyIcon,
  ArrowForward as ArrowForwardIcon,
  ExpandMore as ExpandMoreIcon,
  Hub as HubIcon,
  LocationOn as LocationIcon,
} from '@mui/icons-material'
import {
  VSTRIKE_GRAPH_HIGHLIGHT_EVENT,
  VStrikeAdjacentAsset,
  VStrikeCriticality,
  VStrikeEnrichment,
} from '../../types/vstrike'

interface NetworkContextPanelProps {
  context?: VStrikeEnrichment | null
}

const CRITICALITY_COLOR: Record<
  VStrikeCriticality,
  'default' | 'success' | 'warning' | 'error'
> = {
  low: 'success',
  medium: 'warning',
  high: 'error',
  critical: 'error',
}

function pivotToGraphNode(nodeId: string) {
  window.dispatchEvent(
    new CustomEvent(VSTRIKE_GRAPH_HIGHLIGHT_EVENT, {
      detail: { nodeId },
    })
  )
}

function AttackPathBreadcrumb({
  path,
  currentAssetId,
}: {
  path: string[]
  currentAssetId: string
}) {
  if (!path || path.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No attack path available.
      </Typography>
    )
  }
  return (
    <Stack direction="row" alignItems="center" flexWrap="wrap" gap={0.5}>
      {path.map((asset, idx) => {
        const isCurrent = asset === currentAssetId
        return (
          <Stack
            key={`${asset}-${idx}`}
            direction="row"
            alignItems="center"
            gap={0.5}
          >
            <Chip
              label={asset}
              size="small"
              color={isCurrent ? 'primary' : 'default'}
              variant={isCurrent ? 'filled' : 'outlined'}
              onClick={() => pivotToGraphNode(asset)}
              sx={{ cursor: 'pointer' }}
            />
            {idx < path.length - 1 && (
              <ArrowForwardIcon fontSize="small" color="action" />
            )}
          </Stack>
        )
      })}
    </Stack>
  )
}

function AdjacentAssetChip({ asset }: { asset: VStrikeAdjacentAsset }) {
  const label = asset.edge_technique
    ? `${asset.asset_id} · ${asset.edge_technique}`
    : asset.asset_id
  const title = [
    asset.asset_name ? `Name: ${asset.asset_name}` : null,
    asset.segment ? `Segment: ${asset.segment}` : null,
    `Hop distance: ${asset.hop_distance}`,
    asset.edge_technique ? `MITRE: ${asset.edge_technique}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <Tooltip title={title}>
      <Chip
        label={label}
        size="small"
        variant="outlined"
        onClick={() => pivotToGraphNode(asset.asset_id)}
        sx={{ cursor: 'pointer' }}
      />
    </Tooltip>
  )
}

export default function NetworkContextPanel({
  context,
}: NetworkContextPanelProps) {
  if (!context) return null

  return (
    <Accordion defaultExpanded elevation={2}>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <Box display="flex" alignItems="center">
          <TopologyIcon sx={{ mr: 1, color: 'primary.main' }} />
          <Typography fontWeight="bold">Network Context (VStrike)</Typography>
          <Chip
            label={context.criticality}
            color={CRITICALITY_COLOR[context.criticality] || 'default'}
            size="small"
            sx={{ ml: 1, textTransform: 'capitalize' }}
          />
        </Box>
      </AccordionSummary>
      <AccordionDetails>
        <Paper sx={{ p: 2, bgcolor: 'background.default' }} elevation={0}>
          <Grid container spacing={2}>
            <Grid item xs={12} sm={6}>
              <Typography variant="subtitle2" color="text.secondary">
                Asset
              </Typography>
              <Typography variant="body1">
                {context.asset_name || context.asset_id}
                {context.asset_name && (
                  <Typography
                    component="span"
                    variant="body2"
                    color="text.secondary"
                    sx={{ ml: 1 }}
                  >
                    ({context.asset_id})
                  </Typography>
                )}
              </Typography>
            </Grid>

            <Grid item xs={12} sm={6}>
              <Typography variant="subtitle2" color="text.secondary">
                Segment
              </Typography>
              <Stack direction="row" alignItems="center" gap={1}>
                <HubIcon fontSize="small" color="action" />
                <Typography variant="body1">{context.segment}</Typography>
                {context.site && (
                  <>
                    <LocationIcon fontSize="small" color="action" />
                    <Typography variant="body2" color="text.secondary">
                      {context.site}
                    </Typography>
                  </>
                )}
              </Stack>
            </Grid>

            {context.mission_system && (
              <Grid item xs={12} sm={6}>
                <Typography variant="subtitle2" color="text.secondary">
                  Mission System
                </Typography>
                <Typography variant="body1">{context.mission_system}</Typography>
              </Grid>
            )}

            {typeof context.blast_radius === 'number' && (
              <Grid item xs={12} sm={6}>
                <Typography variant="subtitle2" color="text.secondary">
                  Blast Radius
                </Typography>
                <Typography variant="body1">
                  {context.blast_radius} asset
                  {context.blast_radius === 1 ? '' : 's'} reachable
                </Typography>
              </Grid>
            )}

            <Grid item xs={12}>
              <Divider sx={{ my: 1 }} />
              <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                Attack Path
              </Typography>
              <AttackPathBreadcrumb
                path={context.attack_path}
                currentAssetId={context.asset_id}
              />
            </Grid>

            {context.adjacent_assets && context.adjacent_assets.length > 0 && (
              <Grid item xs={12}>
                <Typography variant="subtitle2" color="text.secondary" gutterBottom>
                  Adjacent Assets
                </Typography>
                <Box display="flex" flexWrap="wrap" gap={1}>
                  {context.adjacent_assets.map((asset) => (
                    <AdjacentAssetChip
                      key={`${asset.asset_id}-${asset.hop_distance}`}
                      asset={asset}
                    />
                  ))}
                </Box>
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={{ mt: 1, display: 'block' }}
                >
                  Click a chip to highlight the corresponding node in the
                  entity graph.
                </Typography>
              </Grid>
            )}

            <Grid item xs={12}>
              <Typography variant="caption" color="text.secondary">
                Enriched by CloudCurrent VStrike at{' '}
                {new Date(context.enriched_at).toLocaleString()}
              </Typography>
            </Grid>
          </Grid>
        </Paper>
      </AccordionDetails>
    </Accordion>
  )
}
