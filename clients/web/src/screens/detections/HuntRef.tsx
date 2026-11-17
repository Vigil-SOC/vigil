import { Link } from 'react-router-dom'
import type { HuntRunLink } from '../../services/api'

/** A hunt named in a gap's evidence: a link to its run, withheld when the run was on a Case
 * the reader can't read, plain text when no run is recorded for it. */
export default function HuntRef({ id, run }: { id: string; run: HuntRunLink | undefined }) {
  if (run?.withheld) return <span className="x1-muted" title="It ran on a Case you can’t read">Hunt withheld</span>
  if (!run?.run_id) return <>Hunt <span className="mono">{id}</span></>
  return <Link className="de-link" to={`/workflows?run=${encodeURIComponent(run.run_id)}`}>Hunt <span className="mono">{id}</span></Link>
}
