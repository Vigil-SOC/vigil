import type { ReactNode } from 'react'

/** A grey placeholder where a number, a line or a control will be. */
function Bone({ w, kind }: { w?: number | string; kind?: 'num' | 'pill' | 'chip' | 'bar' }) {
  return <i className={`de-sk${kind ? ` ${kind}` : ''}`} style={w == null ? undefined : { width: w }} />
}

const KPIS = ['Rules in inventory', 'Declare an ATT&CK technique', 'Rules that can’t fire']
const SIEM_ROWS = [['Elastic', 46], ['Splunk', 38], ['Wazuh', 30]] as const
const SPOTS = [[148, 44], [176, 40], [120, 48], [160, 44]]
const CHIPS = [[128, 96, 104, 112, 106, 90], [104, 84, 116, 94, 82, 128, 118]]
const ROWS = [
  [62, 84, 28], [48, 70, 22], [70, 58, 28], [54, 76, 22], [66, 64, 28], [44, 80, 22], [58, 68, 28],
]

/** Rules while the first listing loads: the page's frame, its labels and headers as
 * they will be, and bones where the counts, bars and rows go. */
export function RulesLoading({ rollout }: { rollout: ReactNode }) {
  return (
    <div className="de-loading" aria-busy="true">
      <div className="de-kpis">
        {KPIS.map((label) => (
          <div key={label} className="de-kpi">
            <span className="de-kpi-label">{label}</span>
            <Bone kind="num" w={88} />
            <Bone w={124} />
          </div>
        ))}
      </div>

      {rollout}

      <div className="de-pair">
        <section className="x1-card de-card de-siems">
          <div className="de-card-head">
            <div>
              <h2>Health by SIEM</h2>
              <p>Whether each enabled rule can actually fire where it is deployed</p>
            </div>
            <div className="de-legend">
              <span><i className="poor" />Can’t fire</span>
              <span><i className="idle" />Not checked</span>
              <span><i className="good" />Can fire</span>
            </div>
          </div>
          {SIEM_ROWS.map(([name, w]) => (
            <div key={name} className="de-siem">
              <div className="de-siem-head"><Bone w={w + 50} /><Bone w={190} /></div>
              <Bone kind="bar" />
            </div>
          ))}
        </section>

        <section className="x1-card de-card de-blind">
          <div className="de-card-head">
            <h2>Blind spots</h2>
          </div>
          <p className="de-card-sub">Data sources that aren’t arriving, and the rules each one starves</p>
          <Bone w={64} />
          <ul className="de-spots">
            {SPOTS.map(([a, b], i) => <li key={i}><Bone w={a} /><Bone w={b} /></li>)}
          </ul>
        </section>
      </div>

      <section className="x1-card de-card de-inv">
        <div className="de-inv-head">
          <div>
            <h2>Rule inventory</h2>
            <p role="status">Reading the inventory…</p>
          </div>
          <div className="de-inv-tools">
            <Bone kind="chip" w={232} />
            <Bone kind="chip" w={300} />
            <Bone kind="chip" w={104} />
          </div>
        </div>
        <div className="de-chips-row de-sk-chips">
          {CHIPS.map((row, i) => (
            <div key={i} className="de-chips">{row.map((w, j) => <Bone key={j} kind="chip" w={w} />)}</div>
          ))}
        </div>
        <div className="de-table">
          <div className="de-tr de-th">
            <span>Rule</span><span>Where</span><span>Technique</span><span className="col-low">Health</span><span className="de-right">Fires</span><span className="de-right">Action</span>
          </div>
          {ROWS.map(([name, why, meta], i) => (
            <div key={i} className="de-tr de-sk-row">
              <div>
                <Bone w={`${name}%`} />
                <Bone w={`${why}%`} />
                <Bone w={`${meta}%`} />
              </div>
              <div className="de-where-cell"><Bone kind="pill" w={52} /><Bone w={96} /></div>
              <div><Bone w={64} /></div>
              <div className="col-low"><Bone kind="pill" w={86} /></div>
              <div className="de-right"><Bone w={28} /></div>
              <div className="de-right"><Bone kind="pill" w={96} /></div>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}

const LENS = [78, 58, 56, 56]
const TACTIC_CHIPS = [70, 62, 70, 112, 98, 104, 66, 104, 64, 128, 76, 54]
const TACTIC_ROWS = [96, 70, 84, 128, 112, 116, 76, 118, 74, 144, 86, 60]

/** Coverage while the map loads: its lens, tactic chips and one row per tactic, as bones. */
export function CoverageLoading() {
  return (
    <div className="x1-card de-cov de-loading" aria-busy="true">
      <div className="de-cov-head">
        <div className="de-cov-lens">
          {LENS.map((w, i) => <Bone key={i} kind="chip" w={w} />)}
          <span className="de-cov-sep" aria-hidden="true" />
          <Bone kind="chip" w={70} />
        </div>
        <span className="x1-muted de-sk-status" role="status">Reading the coverage map…</span>
      </div>
      <div className="de-cov-tactics">
        <span className="x1-muted">Tactics</span>
        {TACTIC_CHIPS.map((w, i) => <Bone key={i} kind="chip" w={w} />)}
      </div>
      <div className="de-tac">
        {TACTIC_ROWS.map((w, i) => (
          <div key={i} className="de-tac-row">
            <div className="de-tac-head">
              <Bone w={w} />
              <Bone kind="bar" />
              <Bone w={236} />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

const METRICS = ['Gap to rule in shadow', 'Shadow to live', 'Live rules proven', 'First week', 'Search budget']

/** The rollout card's cells while its metrics load: each metric's name, bones for the rest. */
export function RolloutLoading() {
  return (
    <ul className="de-rollout-cells" aria-busy="true">
      {METRICS.map((name) => (
        <li key={name}>
          <span className="de-rollout-name">{name}</span>
          <Bone kind="num" w={64} />
          <Bone w="88%" />
          <Bone w="60%" />
          <Bone w="48%" />
        </li>
      ))}
    </ul>
  )
}
