# SOC Console redesign — gaps & follow-up work

> 📋 For the current at-a-glance **"what's left"** checklist, see
> [`REDESIGN_TODO.md`](./REDESIGN_TODO.md). This file is the detailed audit
> history; some §7 "not represented" notes below have since landed (Auto-Ops
> runtime, Login) — the TODO reflects current state.

This documents the UI redesign ported from the **Claude Design** handoff bundle
(`full-screen-design-update`) and, importantly, everything the design **did not
account for** so we can tackle it in future passes.

> The design medium was an HTML/CSS/JS prototype. It was recreated faithfully
> ("pixel-perfect") in React/TSX, mounted as a **standalone preview route**.
>
> **Styling: Tailwind.** The redesign uses Tailwind v3, **scoped to
> `src/redesign`** with **preflight disabled** so it can't reset the rest of the
> MUI app. The reusable design-system primitives (data table, per-row badges,
> chart internals, the dock / tweaks / rail / timeline / master-detail) live in
> `styles.css` under `@layer components` (the idiomatic Tailwind home for
> repeated patterns); screen layout/composition uses utility classes. Everything
> is scoped under `.soc-console`.
>
> **Design-token authority (read before editing tokens).** Tokens live in three
> places and the split matters:
> - **Base palette / radii / surfaces** → `styles.css` `:root` (e.g. `--bg`,
>   `--accent`, `--r`). This is the source of truth for the dark theme.
> - **Accent presets + default + runtime override** → `shell/accent.ts`
>   (`accentVars()`).
> - **Utility exposure only** → `tailwind.config.cjs` re-maps the CSS vars onto
>   Tailwind utilities; it does **not** define values. (The accent stays
>   runtime-swappable because utilities resolve the CSS var at paint.)
>
> **Source of truth for the design itself:** the only pointer today is the bundle
> slug `full-screen-design-update` (an HTML/CSS/JS prototype). ⚠️ There is no
> versioned/locatable reference (repo path, URL, Figma, or pinned revision) to
> diff future passes against — see [§13](#13-planning-metadata--what-this-doc-still-needs).

---

## Status update — 2026-06-22

A second implementation pass closed the **nav shell**, the **chat quick-wins**,
and the four **§8 depth** areas. What changed in this pass:

1. **Nav shell (§2).** The shell now consumes `AuthContext` (`useAuth`). The rail
   has an **account menu** (`shell/UserMenu.tsx` — avatar → name/email/role,
   Settings, Logout via `useAuth().logout()`) and rail items are
   **permission-gated** (`cases.read` / `ai_decisions.approve` / `settings.read`),
   with a screen-level access-denied guard. DEV_MODE grants every permission, so
   the preview is unchanged.
2. **Chat (§5) quick-wins.** The SSE send moved off bare `fetch` onto a new
   exported `streamFetch` helper in `services/api.ts` that applies the same
   machinery as the axios instance — cookie creds, the `X-CSRF-Token`
   double-submit header, and a one-shot 401 → `/auth/refresh` → retry. The
   default model now resolves from the `chat_default` component assignment
   (`aiConfigApi`) instead of the hardcoded constant.
3. **§8 depth.** On-demand finding **AI enrichment** (`getEnrichment` + status
   update + delete) landed in `FindingPopup`; workflow run **detail** (`getRun` →
   result summary / error / per-phase table) became an expandable row in the
   history modal; the Agents tab gained a **New Agent create flow + AI-assist
   generate/refine** (`generateCustom`) on top of the existing fork/edit/delete;
   and a net-new **SLA Policies** settings section (`slaPoliciesApi` CRUD +
   set-default + force-delete) was added.

> ⚠️ The 2026-06-18 audit below had already gone stale by this pass: **routing
> (§1), filters/pagination/faceted-search (§3/§4), and the rail collapse/brand
> (§2) were all completed before 2026-06-22** — the doc just hadn't been updated.
> Section tags below now reflect current code.

### TL;DR — what is still NOT done (current)
- 🔴 **§7 Net-new surfaces** — Orchestrator/Auto-Ops **runtime** page,
  Investigation workspace, VStrike 3D control plane, standalone CostAnalytics
  page, Timesketch sketch-management page, Login, outbound webhooks.
- 🔴 **§12 Theme / integration decision** — canonical-vs-preview still
  unresolved (the lead decision); Tweaks remain in-memory with no production
  counterpart; **`/redesign` is still a public route** (not under
  `ProtectedRoute`), though the shell now respects permissions + has logout.
- 🟡 **§8 (remainder)** — SLA *policy* admin done; per-case relationships/
  evidence/audit-log depth, the finding VStrike `NetworkContextPanel`, and the
  full versioned WorkflowBuilder phase model are still partial.
- 🟢 **§5 Chat** — **deeper parity done (2026-06-22 pass 2):** live USD cost
  band + exact `count_tokens` context bar (debounced `analyticsApi.estimateCost`,
  shown in the composer strip + Status panel), reasoning-trace modal
  (`reasoningApi`), and investigation-keyed dedup (the deterministic seed prompt
  is the dedup key). **Only image/file upload remains** (deliberately deferred).
- 🟢 **§10 Cross-cutting** — **done (2026-06-22 pass 2):** global
  snackbar/toast (`shell/toast.tsx`, scoped under `.soc-console`; Settings now
  routes through it), Chat-panel focus trap (on top of the existing
  `role=dialog`/Esc/focus-return), and the desktop-notification subsystem
  (`shell/useDesktopNotifications.ts` reads `show_notifications`, gates the
  shared service, and fires OS notifications for newly-arrived findings).
  Timeline reduced-motion was already guarded. **a11y pass also done
  (2026-06-22 pass 3):** tab strips carry `role=tab`/`aria-selected`
  (+`tablist`), the `sk-toggle` switches got `role=switch`/`aria-checked`
  + keyboard (shared `activateOnKey`), search/rows-per-page/confidence inputs
  got `aria-label`s, and the div-based decision-list row is now a keyboard
  `role=button`. (Clickable `<tr>`s were intentionally left as table rows —
  each already has a focusable in-row action button.)
- 🟢 **§2 Nav** — **dynamic-membership plumbing now live (2026-06-22 pass 2):**
  the shell fetches enabled integrations once + polls orchestrator status every
  10s, and `NAV` carries an optional `gate` consulted by the rail filter. **No
  item is gated by decision:** Auto Ops stays always-visible (gating it made it
  vanish confusingly) and Timesketch has no redesign screen yet — adding a gate
  is now a one-line `data.ts` change.

---

## Status update — 2026-06-18

This doc was originally written when the redesign was a static, mock-data
preview. **That is no longer true.** Two things changed since:

1. **All six data screens are wired to the real backend.** Every screen fetches
   through a hook in its own folder (`useFindings`, `useAttack`, `useTimeline`,
   `useCases`, `useDecisions`, `useCaseMetrics`, `useAnalytics`,
   `useWorkflowsData`, `useSettings`) that calls a `services/api` client and maps
   the response via `data/mappers.ts`. The hooks follow a uniform
   `phase: 'loading' | 'ready' | 'error'` + `reload()` contract, and screens
   render **loading / empty / error** states. **The illustrative mock datasets
   have been deleted** — `data/data.ts` / `data/appData.ts` /
   `screens/dashboard/attackData.ts` now hold only view-model **types** and real
   display **config** (NAV, TITLES, AGENT_META, SEV_COLOR, …). Nothing in the
   redesign fabricates data anymore.
2. **The folder layout was reorganized into feature folders** (`shell/`,
   `shared/`, `data/`, `screens/<name>/`). See the [File map](#file-map).

A focused audit (2026-06-18) re-checked every claim below against current code.
Sections are now tagged **✅ DONE**, **🟡 PARTIAL**, or **🔴 OPEN**.

### TL;DR — what was still NOT done as of 2026-06-18 (superseded)
> See the **2026-06-22 TL;DR above** for current status; this is the historical
> baseline. Several items here — §1 routing, §3/§4 filters/pagination/search —
> were completed shortly after this audit.
- 🔴 **§1 Routing / URL state** — no router at all; nothing is bookmarkable.
- 🟡 **§2 Nav shell** — static NAV, no user menu/logout, no permission-gating.
- 🟡 **§3/§4 Filters, faceted search, pagination** — still inert (data fetch,
  refresh, sort, row→detail, and the action dialogs are wired).
- 🟡 **§5 Chat** — wired, but raw `fetch` (no CSRF/refresh), no persistence,
  model + thinking hardcoded.
- 🟡 **§6/§7 Net-new surfaces** — full Orchestrator runtime page, Investigation
  workspace, VStrike 3D, standalone CostAnalytics page, Timesketch page, Login.
- 🟡 **§8 Finding enrichment + Agent builder.**
- 🟡 **§10 Cross-cutting** — no global snackbar; partial polling; a11y gaps;
  no notification subsystem. (Error boundary is now done.)
- 🔴 **§12 Theme integration decision** — canonical vs preview still unresolved;
  Tweaks bespoke + in-memory; `/redesign` bypasses auth.

---

## What was built

- **Route:** `/redesign` — full-screen, standalone (outside `MainLayout` /
  `ProtectedRoute`), lazy-loaded. Open `http://localhost:6988/redesign`.
- **Shell** (`SocConsole.tsx`): 60px icon nav rail, slim topbar (title/subtitle),
  main view, right-docked **Vigil chat** that pushes content, floating
  **"Ask Vigil"** FAB, and an **ErrorBoundary** around the active screen. Theme
  state (light/dark + accent) is provided by `shell/theme.tsx`
  (`RedesignThemeProvider`) wrapping the shell and read for `data-theme` + accent
  CSS vars on `.soc-console`.
- **Appearance** (`screens/settings/AppearanceSection.tsx`): light/dark mode +
  accent presets (violet/cyan/emerald/coral) + custom color picker / hex field,
  reached via Settings → Appearance. Mode is the app-wide, backend-persisted
  `ThemeContext` preference; accent persists to `localStorage`. Replaced the old
  floating bottom *Tweaks* panel — its density, adaptive-columns, and
  insights-rail controls were dropped with it. See §12.
- **Six screens, all wired to the backend:**
  - **Dashboard** — tabs: Findings, ATT&CK (table + charts rail + working
    min-confidence slider + range), Timeline (interactive Gantt: zoom / fit /
    play / scrub / speed / CSV export), Entity Graph (**stub**).
  - **Cases** — full-width table → master-detail split with real lifecycle
    actions (SLA / tasks / IOCs / comments / watchers / edit / merge / export).
  - **Case Metrics** — MTTD/MTTR/analyst-performance from `caseMetricsApi`,
    7/30/90-day selector drives the queries.
  - **Analytics** — report + sticky AI-insights rail + attack-time heatmap,
    range-driven.
  - **AI Decisions** — Pending / All / Analytics / Approvals tabs, all wired;
    feedback (quick + detailed) and approve/reject submit to the backend.
  - **Workflows & Skills** — Workflows / Agents / Skills tabs (list + run +
    custom CRUD + skill toggle; some builders partial).
  - **Settings** — left-nav settings surface; General / System / Federation /
    Users / Auto-Investigate / AI Config / Integrations / Developer sections all
    wired to their `configApi` / `*Api` clients.
- **Data pattern:** `useEffect` + shared axios client + `useState` (no
  React-Query anywhere — a deliberate choice matching the rest of the app; see §9).
- **A colocated smoke test** exists: `SocConsole.test.tsx` (mount, nav switching,
  dashboard tabs, master-detail, chat, decisions, skills). It is the only test
  for the redesign.

---

## Things still to build

> Reading guide: **§1–§2** are foundational/structural and block almost
> everything else. **§3–§5** are per-screen behavior. **§6–§9** are whole
> real-app surfaces / cross-cutting wiring. **§10–§11** are cross-cutting
> engineering. **§12–§13** are integration strategy and this doc's own
> planning metadata. See [§13](#13-planning-metadata--what-this-doc-still-needs)
> for recommended sequencing.

### 1. Routing, URL state & deep-linking — ✅ DONE (router landed)
**Update (2026-06-22):** `SocConsole` is now router-driven via `react-router`
(`useNavigate`/`useParams`). Each screen owns a URL (`/redesign/:screen`), Cases
deep-link to a case via `?case=`, there's a real 404 screen, and
back/forward/refresh all work. The thin spots that remain: cross-screen
query-param jumps and the legacy alias routes noted below. *The prose below
describing "no router layer at all" is historical.*

- **In-memory navigation only.** Active screen is `useState<ScreenKey>('dashboard')`
  with a `go()` setter (`SocConsole.tsx:40,57`); per-screen tabs are local state
  too. Consequence: switching screens never changes the URL, browser
  **Back/Forward do nothing**, a **refresh always resets to Dashboard**, and
  **nothing is bookmarkable or shareable**. The real app (`App.tsx`) is fully
  router-driven.
- **No query-param state, so deep-linking and cross-screen jumps are impossible.**
  The real app leans on query params for sub-state *and* inter-page navigation:
  `?tab=` (BuilderTool, Settings), `?agent_id=`/`?investigation_id=` (AIDecisions),
  `?case_id=`/`?finding_ids=`/`?cluster_id=` (Investigation), `?highlight=`
  (Orchestrator). Screens cross-link: Dashboard timeline/graph →
  `/investigation?finding_ids=…`; AIDecisions → `/orchestrator?highlight=`;
  Orchestrator → `/ai-decisions?agent_id=…`. None of this works in the redesign.
- **Legacy redirect/alias routes are unaccounted for.** A unified redesign must
  preserve existing bookmarks: `/analytics/cost` → `/settings?tab=general`,
  `/workflow-builder` → `/builder`, `/users` → `/settings?tab=users`.
- **View state (pagination + filters) is not URL-backed** and resets on every
  screen switch (sort state and tab selection are local `useState`).

**Decision needed:** adopt react-router inside the shell (with nested routing for
per-screen tabs + master-detail selection) vs. keep the in-memory dispatch.

### 2. Navigation shell — 🟢 DONE (membership plumbing live)
**Update (2026-06-22, pass 2):** the dynamic-membership plumbing now matches
production — `SocConsole` fetches enabled integrations once and polls
`orchestratorApi.getStatus()` every 10s, and `NAV` items carry an optional
`gate` (`{ integration }` / `{ orchestrator }`) consulted by the rail filter.
By product decision **no item is gated today**: Auto Ops stays always-visible
(gating it on orchestrator status made it disappear confusingly) and Timesketch
has no redesign screen yet (§7) — both are now a one-line `data.ts` change away
once wanted. *Earlier detail below.*

**Update (2026-06-22):** the rail now has a **collapse/expand toggle + brand**
(done earlier) and an **account menu** (`shell/UserMenu.tsx`: name/email/role,
Settings, Logout via `useAuth().logout()`), and rail items are
**permission-gated** (`cases.read` / `ai_decisions.approve` / `settings.read`)
with a screen-level access-denied guard. **Still open:** dynamic membership
(Timesketch/Auto-Ops by integration/orchestrator status — deferred, those
screens don't exist in the redesign yet) and the Skills/Builder rail split.
*Historical detail below.*

The rail renders and the active screen highlights, but it diverges from production:

- **Nav membership is runtime-dynamic in the real app; redesign `NAV` is static.**
  The Timesketch item renders only when integrations include `timesketch`, and the
  **Auto Ops** item appears/disappears based on orchestrator status (re-polled
  every 10s by `MainLayout`). Redesign `NAV` (`data/data.ts:18-26`) is a static
  7-item tuple with no conditional/polled membership.
- **Missing rail affordances:** the real `NavigationRail` collapses/expands
  (64↔220px), shows a brand label, and renders a **UserMenu (logout/profile)**.
  The redesign rail is fixed-width icon-only with **no expand state, no brand, and
  no user/account/logout control anywhere**.
- **Active highlighting is URL-decoupled.** The redesign computes active from an
  in-memory key, not `useLocation()`. The rail's key-less-item handling (`NAV`
  allows a `null` screen-key and `SocConsole.tsx` only attaches `onClick` when a key
  is present) is currently **unused** — Entity Graph was removed from the rail and
  now lives as a Dashboard tab (its inert "Preview the graph" CTA is the dead
  affordance, see §4). Clean up the stale `data/data.ts` comment and the now-unused
  `| null` in the `NAV` type.
- **Two real destinations merged into one.** "Skills" (`/skills`) and "Builder
  Tool" (`/builder`) are separate top-level nav items; the redesign folds both
  into a single "Workflows & Skills" rail item. Decide whether to restore the split.
- **Permission-aware nav.** Per-route gates in the real app: `cases.read`,
  `ai_decisions.approve`, `settings.read`, `users.read`. The redesign exposes every
  item unconditionally; a future pass should hide/disable rail items the user lacks
  permission for.

### 3. Dialogs / modals — 🟡 PARTIAL (most landed)
**Update (2026-06-22):** the rows the table below marks 🔴 are now mostly ✅ —
**Filters** (`FilterButton`/`FilterGroup` on Findings + Cases), **Advanced /
faceted case search** (`caseSearchApi`), **pagination** ("Rows per page" on
Findings + Cases), and the **New Case** create flow have all landed. Still 🔴:
the bulk **Export / Generate-report** buttons (Dashboard/Analytics) — the
Timeline CSV export works.

The prototype never designed any modal. Most action dialogs are now wired; the
remaining inert affordances are filters/search/pagination/export.

| Affordance (redesign) | Status | Notes |
|---|---|---|
| Click a **finding** row | ✅ | opens `FindingPopup` (fetches `findingsApi.getById`, renders fields + MITRE predictions) |
| **Edit / Merge / Export** case (detail pane) | ✅ | EditCaseDialog / MergeCaseDialog / ExportTimesketchDialog wired |
| Decisions **Approve / Modify / Reject** + feedback | ✅ | quick + detailed feedback → `aiDecisionsApi.submitFeedback`; approvals → `approvalsApi.approve/reject` |
| **New / Run / Delete** workflow (custom) | ✅ | wired via `workflowApi` |
| Skills enable/disable toggle | ✅ | optimistic update + rollback via `skillsApi` |
| **New Case** create wizard | 🔴 | no create-case entry point in the redesign |
| **Build Skill / New Agent** builders | 🟡 | buttons + partial `WorkflowBuilder`; Agent builder absent (WIP) |
| **Filters** button + filter chips | 🔴 | display-only; no filter popovers/menus (needs design) |
| **Advanced Search** (Cases) + faceted search | 🔴 | not wired to `caseSearchApi` (faceted UI undesigned) |
| Pagination ("Rows per page", prev/next) | 🔴 | tables render the full fetched set; no paging |
| **Export / Generate report** buttons (Dashboard/Analytics) | 🔴 | inert (Timeline CSV export **does** work) |
| ATT&CK "Show findings" row chevron | 🟡 | `AttackTechniqueFindings` exists; confirm expand wiring |

### 4. Inert affordances beyond dialogs — 🟡 PARTIAL (mostly resolved)
**Update (2026-06-22):** the "display-only chips" gap is closed — severity /
status / priority / assignee now drive real filter state through the Filters
popover (Findings + Cases). The Entity Graph stub CTA (§7 VStrike) is the main
remaining inert spot.
- ✅ **Refresh buttons** (Findings, ATT&CK, Cases, Workflows, Metrics, Decisions)
  now call their hook's `reload()` → refetch.
- ✅ **Sortable column headers** (Findings, Cases) now sort the fetched data
  client-side via a `SortHeader` + `toggleSort`.
- 🔴 **Display-only chips/spans** (severity, status, priority, MITRE tag, decision
  outcome) still look like the real app's *filterable* chips but are plain spans —
  they need to drive filter state once §3 filters land.
- 🟡 **Per-row action buttons** (Findings "View" eye, Cases "Open" arrow) largely
  duplicate the row click now that the row opens detail.
- 🔴 **Entity Graph stub "Preview the graph" CTA** — no `onClick` (see §7 VStrike).

### 5. Vigil chat — 🟢 DONE (bar image upload)
**Update (2026-06-22, pass 2):** the deeper-parity items are now wired — live USD
cost band + exact `count_tokens` context bar (debounced `analyticsApi.estimateCost`
feeding the composer strip + the Status panel), the **reasoning-trace modal**
(`reasoningApi` session summary + per-interaction list/detail, opened from the
new trace button in the dock head), and **investigation-keyed dedup** (the
deterministic seed prompt is stored as the thread's dedup key, so re-opening the
same finding/case restores the thread instead of duplicating it). **Only
image/file upload remains**, deliberately deferred. *Earlier prose below.*

**Update (2026-06-22):** several items below are resolved — the SSE send uses the
shared `streamFetch` helper (**CSRF + 401-refresh**, no longer bare `fetch`); the
**model defaults from `aiConfigApi` `chat_default`** (no longer hardcoded);
**extended thinking is a persisted setting** (not hardcoded off); and
**conversation history is localStorage-persisted** (History drawer + new-chat
archiving). **Still open (deeper parity):** live USD cost band + exact
`count_tokens` context bar, the reasoning-trace modal (`reasoningApi`),
image/file upload, and investigation-keyed multi-tab dedup. *Original list below.*

`shell/Chat.tsx` POSTs `/api/claude/chat/stream` (SSE), renders `text` events live
with markdown, has a working agent selector (`agentsApi.listAgents`), stop /
new-conversation controls, and graceful error messages. Still open:

- **Raw `fetch`, not the shared axios instance** (`Chat.tsx:166`). It bypasses
  `services/api.ts`, so it skips the **X-CSRF-Token** double-submit header and the
  **401 → `/auth/refresh` → retry** interceptor. With `DEV_MODE` off it would show
  a bare "HTTP 401" with no silent refresh; any future mutating redesign action
  needs the same CSRF/refresh machinery + an authenticated session + Login (§7).
- **Thinking is hardcoded off** (`enable_thinking: false`, `Chat.tsx:173`), so the
  backend never emits thinking events and the "Reasoned for Xs" toggle never shows.
- **Model is hardcoded** to `claude-sonnet-4-6` (`Chat.tsx:31`); the real app drives
  the model from the `chat_default` component assignment (`aiConfigApi`).
- **Auto-selects the Correlator agent** on load (`Chat.tsx:118`) — an undocumented
  default.
- **No persistence:** Header "History"/"More" and per-message "More" have no
  handler; conversations aren't saved.
- **Inert composer extras:** image attach + voice are inert (attach should target
  `claudeApi.uploadFile`); placeholder advertises "/ for commands, @ for context"
  but neither is implemented.
- The production **`ClaudeDrawer`** goes deeper: debounced pre-call USD cost band +
  exact `count_tokens` feeding a 200k context-warning bar; reasoning
  session-summary + per-interaction trace dialog (`reasoningApi`); configurable
  model/max-tokens/extended-thinking/system-prompt (persisted); investigation-keyed
  tab dedup. None of that is in the redesign chat.

### 6. Backend API surface — 🟡 PARTIAL
**Update (2026-06-22):** newly wired — `findingsApi.getEnrichment` (+ `update` /
`delete`), `workflowApi.getRun` (run detail), `agentsApi` create/fork/generate
(`createCustom` / `updateCustom` / `forkAgent` / `generateCustom` /
`getAvailableTools`), and `slaPoliciesApi` (full CRUD + `setDefault`). The §7
runtime clients below (`orchestratorApi` runtime, `reasoningApi`, `vstrikeApi`,
`caseTemplatesApi`, `webhooksApi`, `claudeApi` beyond chat) are still absent.

The read APIs and many write APIs for the six screens **are now wired**:
`findingsApi`, `casesApi` (+ edit/merge), `caseMetricsApi`, `aiDecisionsApi`
(+ `submitFeedback`/`getStats`/`getPendingFeedback`), `approvalsApi`, `workflowApi`,
`agentsApi` (read), `skillsApi` (list/toggle), `attackApi`, `timelineApi`, and —
through Settings — `configApi`, `federationApi`, `llmProviderApi`, `aiConfigApi`,
`budgetsApi`, `mcpApi`, `storageApi`, `localServicesApi`, `detectionRulesApi`,
`kafkaApi`, `ingestionApi`, `orchestratorApi.getStatus`.

**Still absent (no redesign surface):**

| API client | Powers | → |
|---|---|---|
| `orchestratorApi` (runtime) | Auto Ops: investigations CRUD, scan, review, cost, chain-of-custody, export | §7 |
| `reasoningApi` | reasoning session summary + per-interaction/investigation trace | §5/§7 |
| `vstrikeApi` | 3D kill-chain: iframe token, networks, replay, storylines, camera control | §7 |
| `caseSearchApi` | faceted full-text case search + paging | §3/§8 |
| `caseTemplatesApi` | create-case-from-template | §3 |
| `webhooksApi` | outbound webhooks on platform events (CRUD + test) | — |
| `claudeApi` (beyond chat) | `runAgentTask`/`streamAgentTask`, `analyzeFinding`, `generateChatReport`, `uploadFile`, `getModels` | §5/§8 |
| `timesketchApi` (full) | sketch CRUD + local Docker-stack lifecycle (case export **is** wired) | §7 |

### 7. Screens & whole feature surfaces not represented — 🟡 PARTIAL
**Settings now has a redesign analog** (all major sections wired, §13 below), and
the Auto-Investigate **daemon config** lives in Settings. The following heavy
surfaces are still absent or stubbed:

- **Orchestrator / Auto Ops runtime page** (`pages/Orchestrator.tsx`, ~1067 lines):
  the redesign wires the daemon *config* (enable, max-agents, cost guardrails,
  model assignment) but not the **runtime** page — six clickable stat cards that
  filter the investigations table, 10s status poll, live hourly-budget bar, and
  **investigation detail** (per-iteration reasoning trace, chain-of-custody
  timeline, proposed-actions table, tabbed file viewer, human review).
- **Investigation workspace** (`pages/Investigation.tsx`): synchronized
  `EventTimeline` + `EntityVisualization` loaded by mode (`case_id`/`cluster_id`/
  `finding_ids`), view-mode toggle, per-pane fullscreen, **bi-directional
  cross-highlighting**, and **VStrike iframe pivots**. Not represented.
- **VStrike / CloudCurrent 3D control plane** (`vstrikeApi`): kill-chain replay,
  VCR-style storyline playback, 3D camera control. Redesign reduces it to a
  "coming soon" Entity Graph stub.
- **CostAnalytics standalone page** (`pages/CostAnalytics.tsx`, ~560 lines): a
  `CostAnalyticsCard` exists in Settings, but the full page (time-range toggle, KPI
  cards, cost-by-agent + tokens-by-model charts, pricing-source provenance badge,
  admin Recalculate-cost loop) is not ported.
- **Timesketch page** (`pages/Timesketch.tsx`): case export is wired, but the
  sketch list/create/open + local Docker-stack lifecycle is not.
- **Login** — unrepresented; `/redesign` is a public route, so there's no
  sign-in surface even though the shell now reads the session + permission-gates
  (§2, §12).
- **Outbound webhooks** (`webhooksApi`) — not represented.

### 8. Case / finding / decision depth — 🟡 PARTIAL (mostly done)
**Update (2026-06-22):** **Finding enrichment** is now wired — `FindingPopup`
runs on-demand `getEnrichment` (threat summary / risk / impact / actions /
questions / related techniques / IOCs) plus status update + delete (the VStrike
`NetworkContextPanel` is still skipped — it needs the VStrike provider that the
standalone `/redesign` shell doesn't mount). **SLA *policy* admin** is a net-new
Settings section (`slaPoliciesApi` CRUD + set-default + force-delete). The
**Agent builder** gained the create-from-scratch flow + AI-assist
(`generateCustom`) on top of the existing fork/edit/delete. Workflow run
**detail** (`getRun`) is now an expandable history row. **Still partial:**
per-case relationships/evidence/audit-log depth and the full versioned
WorkflowBuilder phase model.
- ✅ **Cases detail** is now a real lifecycle surface: **SLA** card (live
  countdown), **tasks**, **IOCs**, **comments**, **watchers**, plus **edit**,
  cross-case **merge**, and **export to Timesketch** dialogs (`CaseSections.tsx`,
  `CasesScreen.tsx`). Confirm coverage of the full legacy set (relationships,
  evidence, audit log, structured close, escalate, bulkUpdate) — some may remain.
- ✅ **AI Decisions** — all four tabs wired with distinct data; feedback (quick +
  detailed grading modal) and approvals approve/reject submit to the backend;
  Analytics tab renders `getStats` KPIs + outcome distribution.
- ✅ **Case Metrics** — MTTD/MTTR/analyst-performance from `caseMetricsApi`, with
  the 7/30/90-day selector driving queries.
- 🟡 **SLA policy admin** (`slaPoliciesApi`: policy CRUD, set-default per priority,
  usage) — the per-case SLA card is wired, but policy administration is not.
- 🔴 **Case search** (`caseSearchApi`): basic input + "Advanced Search" still not
  wired to faceted full-text search (§3).
- 🟡 **Finding detail** (`FindingPopup`): shows normalized fields + MITRE
  prediction chips, but **on-demand AI enrichment** (`findingsApi.getEnrichment`,
  `force_regenerate` → threat summary/risk/impact/recommended actions/related
  techniques) and the embedded VStrike `NetworkContextPanel` are not ported. The
  per-finding `update`/`delete`/`export` surface is also absent.
- 🟡 **Workflows / Agents / Skills builders:** the real `WorkflowBuilder.tsx`
  (~1670 lines) models ordered phases bound to an agent + tool set, per-phase
  `approval_required` gates, versioning, and run-status/history — the redesign
  `WorkflowBuilder` is partial. `agentsApi` fork/AI-generate/run/investigate and the
  Agent builder are not wired (WIP).

### 9. Going-live data plumbing — ✅ DONE (was "No real data")
This section is the one most changed by recent work.

- ✅ **Read APIs wired** for all six screens (`findingsApi`, `casesApi`,
  `caseMetricsApi`, `aiDecisionsApi`, `workflowApi`, `agentsApi`, `skillsApi`,
  `attackApi`, `timelineApi`). The Analytics screen calls `/analytics` +
  `/analytics/insights` (note: `analyticsApi` in `services/api.ts` is *cost
  estimation* for the chat composer — **not** this screen).
- ✅ **Mock data deleted.** `data/data.ts`, `data/appData.ts`, and
  `screens/dashboard/attackData.ts` now contain only view types + display config.
  KPI cards are **computed** from API responses (`useDashboardKpis`), not hardcoded
  literals.
- ✅ **Async UX states** — hooks expose `phase: 'loading' | 'ready' | 'error'` +
  `error` + `reload()`, and screens render loading / empty / error (retry) states.
- ✅ **Timestamp strategy resolved** — `date-fns` `format()` is used throughout
  (`mappers.ts`, `useCases.ts`, `CaseSections.tsx`, `DecisionsScreen.tsx`, …);
  relative ages computed from ISO timestamps; SLA countdowns recomputed live.
- 🟡 **Data-fetching strategy decided as `useEffect` + axios** (no React-Query
  anywhere in the app). Consequence retained: no cache invalidation,
  retry/backoff, or optimistic updates beyond what each hook hand-rolls (the Skills
  toggle does its own optimistic update + rollback). Revisit if React-Query is
  adopted app-wide (it's CLAUDE.md's stated convention but currently unused).

### 10. Cross-cutting UX & engineering — 🟢 DONE (minor a11y nits)
**Update (2026-06-22, pass 2):** the snackbar, the Chat focus trap, and the
notification subsystem all landed. Global toast lives in `shell/toast.tsx`
(scoped under `.soc-console`, `useToast()` hook, `role=alert`/`status` +
aria-live, auto-dismiss); Settings now routes its save/test feedback through it
(local banner removed). The Chat dock gained a Tab focus trap on top of its
existing `role=dialog`/Esc/focus-return, and its Esc no longer closes the dock
when one of its Popups is open. Desktop notifications are wired via
`shell/useDesktopNotifications.ts`: it reads `show_notifications`, gates the
shared `notificationService`, and fires OS notifications for newly-arrived
findings (baselining the backlog, capped per tick). Timeline reduced-motion was
already guarded. *Earlier per-item list below; only the div-control ARIA-label
nit remains open.*

- 🔴 **No toast/snackbar result-feedback surface.** There's still no global
  success/failure reporting for wired actions. Settings uses a local banner; Cases/
  Decisions use inline error divs; Chat's error line is chat-scoped. A successful
  save/approve has no confirmation UX — the user infers it from the data refresh.
  **Add a shared snackbar provider (scoped under `.soc-console`).**
- ✅ **React error boundary** — `shell/ErrorBoundary.tsx` wraps the active screen
  (`SocConsole.tsx:124`) with a `resetKey` that recovers on screen change and a
  "Try again" affordance.
- 🟡 **Polling / live updates** — partial: Federation settings refresh every 10s,
  Kafka status every 5s, the SLA card ticks every 1s (cosmetic). **No dashboard
  opt-in auto-refresh** and the nav membership isn't polled (§2).
- 🔴 **Accessibility regression from dropping MUI.** Raw `div`/`button` + Tailwind
  with preflight/components off forfeits MUI's ARIA/focus/keyboard affordances. The
  right-docked **Chat** and bottom **Tweaks** panels are state-toggled divs with
  **no `role=dialog`, no focus trap, no Esc-to-close, no focus return**; div-based
  controls lack ARIA labels; the **Timeline** is pointer-only and runs a `rAF`
  playback loop with **no `prefers-reduced-motion` guard**; the **accent presets +
  custom hex picker do no WCAG contrast check**.
- 🔴 **Notification subsystem absent.** The real app's notifications are
  **browser/desktop (OS) notifications** (`services/notifications.ts` +
  `NotificationContext`), gated by the Settings `show_notifications` toggle +
  browser permission and fired by feature components. ⚠️ **Neither UI has an
  in-app bell / unread badge** — the legacy chrome (`MainLayout`/`NavigationRail`)
  has none either, so a notification *center* is net-new for both. The redesign
  exposes the `show_notifications` toggle in General settings but **no consumer
  acts on it**, and it has no browser-notification service.

### 11. Known limitations carried over from the prototype — 🟡 PARTIAL
- ✅ ATT&CK **time-range** and Analytics **range** tabs now drive the data
  (`useAttack(minConfidence, range)`, `useAnalytics(timeRange)`) — no longer
  visual-only.
- ✅ The toolbar/bar-row wrap was tightened (2026-06-22 pass 3): the search box
  now shrinks (its 240px floor drops to 160/120px) instead of forcing a
  half-empty wrap, and `.bar-row` gap/padding tighten below 1024px. Media-gated
  + unlayered, so the desktop layout is untouched.
- 🔴 Layout targets desktop; not designed for mobile/tablet (full responsive
  pass still out of scope — the above is just the narrow-toolbar polish).

### 12. Theme / app integration strategy — 🔴 OPEN (decision needed)
**Update (2026-06-22):** the shell now consumes `AuthContext` (account menu +
permission-gated nav + logout), so it is no longer *auth-blind* — but the
**`/redesign/:screen` route is still public** (not wrapped in `ProtectedRoute`).
The canonical-vs-preview decision and a real Login (§7) are still required.
**Theme is now partly resolved (2026-06-22):** light/dark is bridged to the
app-wide, backend-persisted MUI `ThemeContext` (shared with the legacy UI), the
redesign ships a **light theme** (`.soc-console[data-theme="light"]`) alongside
dark, accent persists to `localStorage`, and the bespoke density /
adaptive-columns / insights-rail tweaks were **removed** (the Appearance settings
page replaced the floating Tweaks panel).

- The redesign now shares **light/dark mode** with the app's MUI `ThemeContext`
  (backend-persisted) and ships both themes; accent (violet default) is still
  redesign-only (`localStorage`). The remaining **lead decision** is canonical
  vs preview for the *styling system itself*: **(a)** make the Tailwind+CSS-vars
  shell canonical and retheme MUI globally, or **(b)** keep it a preview.
- **Option (a) has no migration/rollout plan.** Needed: an inventory of MUI
  `ThemeContext` touchpoints; a strategy to reconcile two styling systems
  (Tailwind + CSS-vars, preflight off, scoped to `.soc-console` vs. MUI's cyan
  light/dark + `configApi.setTheme` persistence); a reusable-vs-rework inventory for
  the §3/§7 dialogs (dropping MUI dialogs into the MUI-disabled `.soc-console` shell
  is non-trivial — the wired ones currently render inside the scoped shell, so audit
  their styling); and a phased/feature-flag rollout.
- **The bespoke Tweaks were removed, not wired.** Density, adaptive columns, and
  insights-rail mode had **no production counterpart**, so they were dropped.
  Light/dark now uses the app-wide persisted `configApi.setTheme` preference;
  accent persists to `localStorage`. No backend/MUI accent field exists (the real
  app's accent is **fixed** cyan), so cross-device accent remains a follow-up.
- `/redesign` **is still a public route** (not under `ProtectedRoute`). The shell
  now permission-gates *nav items + screens* via `useAuth().hasPermission`
  (`cases.read`, `ai_decisions.approve`, `settings.read`) and has a logout, but
  the route itself remains reachable without a session — a real Login (§7) +
  route gating is still needed before this can be canonical.

### 13. Planning metadata — what this doc still needs
- **No prioritization / sizing / owners / acceptance criteria.** Items name the
  target API/component but omit a definition-of-done.
- **Test coverage broadened (2026-06-22 pass 3).** `SocConsole.test.tsx` now
  also covers the **chat SSE stream** (mocked streaming body → rendered reply),
  the **decision feedback mutation** (`submitFeedback` via the inline review
  pane), and the **Timeline CSV export** (stubbed object-URL → CSV Blob). 13
  tests total. Still uncovered: the approvals approve/reject path and the
  detailed-feedback grading modal.
- **No versioned design source-of-truth link** (see intro).

**Recommended sequencing (updated 2026-06-22):**
1. **Decide §12(a) vs (b)** — still the lead decision; gates a real Login + route
   gating for `/redesign`.
2. **§10 shared primitives** — global snackbar/toast + a11y baseline (focus
   trap/Esc on the Chat & Tweaks panels, reduced-motion on the Timeline). Highest
   leverage of the remaining cross-cutting work. (Error boundary is done.)
3. **§7 net-new surfaces** (Orchestrator runtime, Investigation, VStrike,
   CostAnalytics page, Timesketch page, Login, webhooks) — largest scope,
   schedule explicitly.
4. **§5 chat deeper parity** (cost band, reasoning traces, upload) + **§8
   remainder** (per-case depth, VStrike finding panel, versioned WorkflowBuilder).

> Done since this list was first written: **§1** routing, **§2** user-menu +
> permission-gating, **§3/§4** filters / pagination / faceted search / New Case,
> **§5** chat quick-wins, and the **§8** enrichment / SLA admin / agent-builder /
> run-detail items.

---

## Intentional deviations from the prototype (faithful, but worth noting)
- Prototype global rules (`:root`, `body`, `*`, scrollbars, element selectors) were
  scoped under `.soc-console` so the dark theme / `overflow:hidden` can't leak into
  the rest of the MUI app. Class-based rules are verbatim. The stylesheet only loads
  on the lazy `/redesign` route.
- The interactive Timeline keeps the prototype's exact layout math but uses a
  `ResizeObserver` + `requestAnimationFrame` + React refs instead of `innerHTML`.
- Fonts: IBM Plex Sans/Mono loaded via `@import` at the top of `styles.css` (`:1`).

---

## File map
```
frontend/tailwind.config.cjs   Tailwind config (scoped to src/redesign, preflight off; re-exposes CSS vars as utilities)
frontend/postcss.config.cjs    PostCSS: tailwindcss + autoprefixer
frontend/src/redesign/
  SocConsole.tsx       shell: rail, topbar, view router, chat dock, FAB, error boundary
  SocConsole.test.tsx  smoke test: mount, nav, dashboard tabs, master-detail, chat, decisions, skills
  styles.css           Tailwind directives + scoped .soc-console root (base tokens in :root) + design system in @layer components; IBM Plex @import at line 1

  shell/               app-shell-only pieces
    Chat.tsx           Vigil chat dock (real SSE via streamFetch; see §5 for open items)
    UserMenu.tsx       rail account menu (name/email/role, Settings, Logout) — useAuth
    theme.tsx          RedesignThemeProvider (light/dark + accent → data-theme/CSS vars)
    toast.tsx          global snackbar (useToast, scoped under .soc-console; §10)
    Loader.tsx         redesign-styled Suspense fallback (used by App.tsx for /redesign)
    useDesktopNotifications.ts  OS notifications for new findings, gated by settings (§10)
    ErrorBoundary.tsx  wraps the active screen, resets on screen change
    accent.ts          accent presets + hex/lighten helpers + accentVars()

  shared/              cross-screen primitives
    ui.tsx             shared UI primitives (select, Rating, Slider, etc.)
    VigilLogo.tsx      inline-SVG brand lockup + mark (recolor via currentColor)
    icons.tsx          Icon component + ICON path map
    charts.tsx         Donut / Spark / Trend / Hbars / Heatmap (inline-SVG, accent-aware)
    Markdown.tsx       markdown renderer (chat + case/workflow text)
    types.ts           ScreenProps contract + shared view types

  data/                view-model types + display config + API→view mappers (NO mock data)
    data.ts            ScreenKey, NAV, TITLES + Finding/CaseRow types
    appData.ts         AGENT_META, prettyHandle + Workflow/Decision/AgentTemplate/Skill types
    mitre.ts           MITRE technique→tactic lookup (reference data)
    mappers.ts         map real API responses (snake_case) → view shapes

  screens/<name>/      each screen + its private hooks/components
    dashboard/         DashboardScreen, AttackTechniqueFindings, FindingPopup,
                       attackData (timeline types/config), useAttack, useFindings, useTimeline
    cases/             CasesScreen, CaseSections (SLA/tasks/IOCs/comments/watchers cards), useCases
    decisions/         DecisionsScreen, useDecisions
    metrics/           MetricsScreen, useCaseMetrics
    analytics/         AnalyticsScreen, useAnalytics
    workflows/         WorkflowsScreen, WorkflowBuilder, useWorkflowsData
    autoops/           AutoOpsScreen, InvestigationDetail, statusBadge, useAutoOps, useInvestigationDetail
    login/             LoginScreen (routed at /redesign/login via App.tsx)
    notfound/          NotFoundScreen (in-shell 404)
    settings/          SettingsScreen, useSettings + section components (General/System/
                       Federation/Users/Appearance/SlaPolicies/AutoInvestigate/AiConfig/
                       Integrations/Developer + dialogs); useSlaPolicies hook for the SLA section
```
