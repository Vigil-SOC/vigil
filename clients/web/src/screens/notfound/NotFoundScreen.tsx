import { Icon } from '../../shared/icons'

export default function NotFoundScreen({
  path,
  onHome,
  homeLabel = 'Home',
}: {
  path?: string
  onHome: () => void
  homeLabel?: string
}) {
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
        <Icon name="home" /> Back to {homeLabel}
      </button>
    </div>
  )
}
