/* ============================================================
   404 — shown inside the SOC console shell when a /<screen>
   path doesn't match a known screen. The nav rail stays available so
   the user can jump elsewhere; this also offers a direct route home.
   ============================================================ */
import { Icon } from '../../shared/icons'

export default function NotFoundScreen({ path, onHome }: { path?: string; onHome: () => void }) {
  return (
    <div className="notfound">
      <div className="nf-code">404</div>
      <h2 className="nf-title">Page not found</h2>
      <p className="nf-sub">
        {path ? (
          <>
            There’s nothing at <span className="mono nf-path">/{path}</span>.
          </>
        ) : (
          'That page doesn’t exist.'
        )}{' '}
        It may have moved, or the link is wrong.
      </p>
      <button className="btn primary" onClick={onHome}>
        <Icon name="grid" /> Back to dashboard
      </button>
    </div>
  )
}
