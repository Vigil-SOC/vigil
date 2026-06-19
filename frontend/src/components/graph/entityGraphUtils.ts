export function getGraphEndpointId(nodeOrId: unknown): string {
  if (typeof nodeOrId === 'object' && nodeOrId !== null && 'id' in nodeOrId) {
    return String((nodeOrId as { id: unknown }).id)
  }
  return String(nodeOrId)
}
