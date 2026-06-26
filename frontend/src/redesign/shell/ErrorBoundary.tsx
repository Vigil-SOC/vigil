/* ============================================================
   Scoped error boundary for the SOC console. A render throw in any
   screen would otherwise blank the whole console; this catches it
   and shows a recoverable fallback. React error boundaries must be
   class components.
   ============================================================ */
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  /** changing this value (e.g. the active screen key) resets the boundary */
  resetKey?: unknown
}
interface State {
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidUpdate(prev: Props) {
    // recover automatically when the user navigates to another screen
    if (this.state.error && prev.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('[redesign] screen render error', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (error) {
      return (
        <div className="screen-error" role="alert">
          <h2>Something went wrong rendering this view.</h2>
          <p>{error.message || 'Unknown error'}</p>
          <button className="btn" onClick={() => this.setState({ error: null })}>
            Try again
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
