import { useState } from 'react'
import { Popup } from '../../shared/ui'
import { errorText } from '../../shared/errorText'
import { detectionRulesApi } from '../../services/api'

/** The server takes at most this many rule copies in one export. */
const MAX_RULES = 2000

type Scope = 'coverage' | 'view'

/** Read a blob error body back as JSON, so errorText sees the server's detail. */
async function unblob(e: unknown) {
  const res = (e as { response?: { data?: unknown } })?.response
  if (res?.data instanceof Blob) {
    try { res.data = JSON.parse(await res.data.text()) } catch { /* not JSON */ }
  }
  return e
}

function save(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

/** Export a proof card: one zip with the signed JSON for machines and its PDF for
 * people, from the same data and the same signature. */
export default function ProofCardExport({
  open,
  onClose,
  viewName,
  viewCount,
  viewIds,
}: {
  open: boolean
  onClose: () => void
  /** The inventory's view, as its heading names it. */
  viewName: string
  viewCount: number
  /** Every rule copy the view lists, loading the pages not yet shown. */
  viewIds: () => Promise<number[]>
}) {
  const [scope, setScope] = useState<Scope>('coverage')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const tooMany = viewCount > MAX_RULES

  const close = () => { if (!busy) { setError(null); onClose() } }

  const download = async () => {
    setBusy(true)
    setError(null)
    try {
      const ids = scope === 'view' ? (await viewIds()).slice(0, MAX_RULES) : undefined
      const res = await detectionRulesApi.proofCard(ids).catch(async (e) => { throw await unblob(e) })
      const name = /filename="([^"]+)"/.exec(String(res.headers['content-disposition'] ?? ''))?.[1] ?? 'vigil-proof-card.zip'
      save(res.data, name)
      onClose()
    } catch (e) {
      setError(errorText(e, 'The proof card did not export.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Popup open={open} onClose={close} title="Export proof card" width={520}>
      <div className="de-pc">
        <fieldset className="de-pc-scope">
          <legend>What to export</legend>
          <label>
            <input type="radio" name="pc-scope" checked={scope === 'coverage'} onChange={() => setScope('coverage')} />
            <span><b>Coverage report</b><span>Proven coverage by ATT&amp;CK technique and SIEM over every live rule, with the rule behind each cell</span></span>
          </label>
          <label>
            <input type="radio" name="pc-scope" checked={scope === 'view'} disabled={viewCount === 0 || tooMany} onChange={() => setScope('view')} />
            <span>
              <b>The rules in this view</b>
              <span>
                {viewCount === 0 ? `${viewName} lists no rule` : tooMany ? `${viewName} lists ${viewCount.toLocaleString()} rules; narrow it to ${MAX_RULES.toLocaleString()} or fewer` : `${viewName} · ${viewCount.toLocaleString()} ${viewCount === 1 ? 'rule' : 'rules'}`}
              </span>
            </span>
          </label>
        </fieldset>

        <div>
          <h4>What’s in the zip</h4>
          <ul>
            <li><code>proof-card.pdf</code> to read and print: per rule, its checks with result, reason and when, and its path to live</li>
            <li><code>proof-card.json</code>, the same data in the published schema proof-card/v1</li>
            <li><code>proof-card.json.sig</code>, an Ed25519 signature over the JSON’s exact bytes</li>
            <li>Vigil’s public key and a README on checking it</li>
          </ul>
        </div>
        <div>
          <h4>How it’s checked</h4>
          <ul>
            <li>The PDF prints the JSON’s SHA-256 and its signature, so a printed copy can be checked too</li>
            <li><code>vigil-verify</code>, or any Ed25519 library, checks the signature with this Vigil’s public key</li>
          </ul>
        </div>

        {error && <p className="de-pc-err" role="alert">{error}</p>}
        <div className="de-pc-actions">
          <button type="button" className="btn ghost" onClick={close} disabled={busy}>Cancel</button>
          <button type="button" className="btn primary" onClick={download} disabled={busy}>{busy ? 'Exporting…' : 'Download proof card'}</button>
        </div>
      </div>
    </Popup>
  )
}
