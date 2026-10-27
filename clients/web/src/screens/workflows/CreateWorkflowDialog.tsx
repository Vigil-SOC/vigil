// How a new workflow starts: built by hand, from a template, or described in words
// for Vigil to draft. All three end in the builder, where it is checked and saved.
import { Icon } from '../../shared/icons'
import { Popup } from '../../shared/ui'
import { WORKFLOW_TEMPLATES, type WorkflowTemplate } from './workflowTemplates'
import './builder.css'

export default function CreateWorkflowDialog({ onClose, onBuild, onTemplate, onDescribe }: {
  onClose: () => void
  onBuild: () => void
  onTemplate: (t: WorkflowTemplate) => void
  onDescribe: () => void
}) {
  return (
    <Popup open onClose={onClose} title="New workflow" width={760}>
      <div className="flex flex-col gap-3.5">
        <div className="wc-grid">
          <button type="button" className="wc-card main" onClick={onBuild} autoFocus>
            <b><Icon name="flow" size={15} /> Build it yourself</b>
            Drag agents onto a flow, put them in order, choose each step’s tools and where a person approves.
          </button>
          <button type="button" className="wc-card" onClick={onDescribe}>
            <b><Icon name="sparkle" size={15} /> Describe it</b>
            Say how your team works the case. Vigil drafts the steps and opens them in the builder to check.
          </button>
        </div>
        <p className="wc-sub">Or start from a template</p>
        <div className="wc-tpls">
          {WORKFLOW_TEMPLATES.map((t) => (
            <button key={t.id} type="button" className="wc-tpl" onClick={() => onTemplate(t)} aria-label={`Use the ${t.name} template`}>
              <b>{t.name}</b>
              <span>{t.summary}</span>
              <span className="wc-tpl-steps">
                {t.phases.map((p, i) => <span key={i}>{i + 1}. {p.name}{p.approval_required ? ' · approval' : ''}</span>)}
              </span>
            </button>
          ))}
        </div>
      </div>
    </Popup>
  )
}
