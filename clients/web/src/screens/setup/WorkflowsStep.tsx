import { useEffect, useState } from 'react'
import { workflowApi } from '../../services/api'

interface WorkflowRow {
  id: string
  name: string
  description: string
}

export default function WorkflowsStep() {
  const [rows, setRows] = useState<WorkflowRow[]>([])
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading')

  useEffect(() => {
    let live = true
    workflowApi
      .listAll()
      .then((res) => {
        if (!live) return
        const list = (res.data?.workflows || []) as {
          id: string
          name?: string
          description?: string
        }[]
        setRows(
          list.map((workflow) => ({
            id: workflow.id,
            name: workflow.name || workflow.id,
            description: workflow.description || '',
          })),
        )
        setPhase('ready')
      })
      .catch(() => {
        if (live) setPhase('error')
      })
    return () => {
      live = false
    }
  }, [])

  if (phase === 'loading') {
    return <p className="text-tx-3 text-sm">Loading workflows…</p>
  }
  if (phase === 'error') {
    return <p className="text-sm text-high">Could not read workflows.</p>
  }
  if (rows.length === 0) {
    return <p className="text-tx-3 text-sm">No workflows yet.</p>
  }

  return (
    <div className="flex flex-col gap-2">
      {rows.map((workflow) => (
        <div key={workflow.id} className="text-sm">
          <div className="text-tx font-medium">{workflow.name}</div>
          {workflow.description && (
            <div className="text-tx-3 text-xs mt-0.5">{workflow.description}</div>
          )}
        </div>
      ))}
    </div>
  )
}
