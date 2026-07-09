/* Mock LogLM admin web component — a vanilla-JS stand-in for the connector's
   real bundle. Registers the <loglm-admin> custom element, reads the host
   context Vigil injects (theme tokens + session + apiBase), renders inside a
   shadow root, loads the fixture metrics from the BFF, and emits the
   `vigil:extension` events Vigil's ExtensionHost listens for. */

const TAG = 'loglm-admin'

class LoglmAdmin extends HTMLElement {
  constructor() {
    super()
    this._ctx = null
    this.attachShadow({ mode: 'open' })
  }

  // Vigil sets the context as a JS property, typically BEFORE the element is
  // appended (to seed the first render) and again after (theme/token refresh).
  // Only (re)render once we're actually in the DOM, so a pre-mount context set
  // doesn't race the connectedCallback render (which would strand the in-flight
  // metrics fetch on a detached node).
  set hostContext(ctx) {
    this._ctx = ctx
    if (this.isConnected) {
      this.render()
      this.loadMetrics()
    }
  }
  get hostContext() {
    return this._ctx
  }

  connectedCallback() {
    this.render()
    this.loadMetrics()
    // tell the host we're mounted and ready
    this.emit({ type: 'ready' })
  }

  emit(detail) {
    this.dispatchEvent(
      new CustomEvent('vigil:extension', { detail, bubbles: true, composed: true }),
    )
  }

  render() {
    const ctx = this._ctx || {}
    const accent = (ctx.themeTokens && ctx.themeTokens['--accent']) || '#7d74f3'
    const mode = (ctx.themeTokens && ctx.themeTokens.mode) || 'dark'
    const user = (ctx.session && ctx.session.user) || 'unknown'
    const bg = mode === 'light' ? '#ffffff' : '#0f1117'
    const fg = mode === 'light' ? '#1a1a1a' : '#e6e6e6'
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; height:100%; font-family: system-ui, sans-serif; }
        .wrap { padding:24px; color:${fg}; background:${bg}; height:100%; box-sizing:border-box; }
        h2 { margin:0 0 4px; }
        .accent { color:${accent}; }
        code { opacity:.8; }
        .card { border:1px solid ${accent}55; border-radius:12px; padding:16px; margin-top:16px; }
        button { background:${accent}; color:#fff; border:0; border-radius:8px; padding:8px 12px; cursor:pointer; margin-right:8px; }
        pre { white-space:pre-wrap; font-size:12px; opacity:.85; margin:0; }
      </style>
      <div class="wrap">
        <h2>LogLM Model Admin</h2>
        <div>Signed in as <b>${user}</b> · apiBase <code>${ctx.apiBase || '—'}</code></div>
        <div class="card"><div id="metrics">Loading model performance…</div></div>
        <div style="margin-top:16px;">
          <button id="notify">Toast host</button>
          <button id="full">Toggle full view</button>
          <button id="err">Report error</button>
        </div>
      </div>
    `
    const $ = (id) => this.shadowRoot.getElementById(id)
    $('notify').onclick = () =>
      this.emit({ type: 'notify', payload: { severity: 'info', message: 'Hello from LogLM (mock)' } })
    $('full').onclick = () => this.emit({ type: 'setViewFull', payload: { full: true } })
    $('err').onclick = () => this.emit({ type: 'error', payload: { message: 'Simulated extension error' } })
  }

  async loadMetrics() {
    const ctx = this._ctx
    if (!ctx || !ctx.apiBase) return
    const el = this.shadowRoot.getElementById('metrics')
    if (!el) return
    try {
      const res = await fetch(ctx.apiBase.replace(/\/$/, '') + '/model-performance', {
        headers: ctx.session && ctx.session.token ? { Authorization: 'Bearer ' + ctx.session.token } : {},
      })
      if (!res.ok) {
        el.textContent = 'Metrics unavailable (' + res.status + ')'
        return
      }
      const data = await res.json()
      el.innerHTML = '<pre>' + JSON.stringify(data, null, 2) + '</pre>'
    } catch (e) {
      el.textContent = 'Metrics fetch failed: ' + e
    }
  }
}

if (!customElements.get(TAG)) customElements.define(TAG, LoglmAdmin)
