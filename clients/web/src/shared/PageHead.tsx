import './kit.css'

/** Title and one-line description at the top of a Settings section. */
export function PageHead({ title, description }: { title: string; description: string }) {
  return (
    <header className="page-head">
      <h2>{title}</h2>
      <p>{description}</p>
    </header>
  )
}
