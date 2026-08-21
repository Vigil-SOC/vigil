# SOC Console — TODO (what's actually left)

At-a-glance checklist of remaining work. The detailed audit history lives in
[`CONSOLE_GAPS.md`](./CONSOLE_GAPS.md); this file is the current "what's left" view.
Section tags (§) reference the gaps doc.

Reconciled against the code on **2026-08-21**, when `src/redesign/` was flattened into
`src/`. Items below are marked either **[verified 2026-08-21]** — re-checked against the
code on that date — or **[carried forward]** — inherited from the 2026-06-22 pass and
*not* re-verified. Treat the carried-forward items as leads, not facts.

> **Paths.** There is no `src/redesign/` any more. The console shell is `src/shell/`,
> its views are `src/screens/`, and the route guards plus the transition loader are
> `src/routing/`. Older notes in the gaps doc still use the pre-flatten paths.

---

## 🟡 Net-new surfaces still missing (§7)

- [ ] **Outbound webhooks** — **[verified 2026-08-21]** and worse than previously
      recorded. This was listed as a missing *frontend* surface; the blocker is the
      backend. `core/integrations/webhooks_router.py` mounts six authenticated routes
      under `/api/webhooks` that persist nothing and still report success —
      `POST /` returns HTTP 200 with a fabricated `webhook_id` of `webhook-001` and
      `"message": "Webhook management coming soon"`. There is no ORM model and no init
      SQL. A caller checking the status code concludes a case-event webhook exists.
      Smallest honest fix, worth doing independently of any UI: return `501` from those
      six routes, or unmount the router. Inbound receivers are unaffected and correctly
      separated (see `webhooks_router.py:14-17`).
- [ ] **VStrike / CloudCurrent 3D control plane** — kill-chain replay, storyline
      playback, camera control (`vstrikeApi`). **[carried forward]**
- [ ] **Timesketch sketch-management page** — case export is wired; sketch
      list/create/open + local Docker-stack lifecycle is not. **[carried forward]**
- [ ] **Full Investigation workspace** — Auto-Ops `InvestigationDetail` landed, but the
      synchronized `EventTimeline` + `EntityVisualization` with bi-directional
      cross-highlighting and VStrike iframe pivots is still partial. **[carried forward]**

## 🟡 Per-screen depth (§8)

- [ ] **Entity Graph is a permanent empty state** — **[verified 2026-08-21]**.
      `screens/dashboard/DashboardScreen.tsx:386` renders `EntityStub`, whose copy
      ("appears here once findings include entity fields") implies a data-availability
      problem when there is no rendering code at all — a user with complete entity data
      sees the same screen as one with none. `@xyflow/react` is already bundled for
      `WorkflowBuilder`, so a graph library is available. Check first whether `core/`
      already computes the relationships (`CONTEXT.md` describes a finding-level
      `graph_builder`); this may be rendering work rather than modelling work.
- [ ] **Cost analytics depth** — **[verified 2026-08-21]**; the previous entry
      ("only a card exists, the full page is not ported") overstated it.
      `screens/settings/CostAnalyticsCard.tsx` already has the range selector, pricing
      provenance, the usage table and loading/error/empty states. Actually missing:
      cost-by-agent / tokens-by-model charts; `POST /analytics/recalculate-cost`
      (`services/api.ts:978`) and `GET /analytics/budget` (`:948`, `:951`) are defined
      with **no caller**; and the placement question — a cost view an operator checks
      routinely may not belong inside Settings. Decide placement before adding more to
      the card.
- [ ] Per-case **relationships / evidence / audit-log**, structured close, escalate,
      bulkUpdate. **[carried forward]**
- [ ] Finding **VStrike `NetworkContextPanel`** — needs a VStrike provider that nothing
      currently mounts. **[carried forward]**
- [ ] **Versioned `WorkflowBuilder`** phase model (ordered phases → agent + tool set,
      per-phase approval gates, versioning, run history). **[carried forward]**

## 🟡 Smaller gaps in already-wired screens

- [ ] **Export / Generate-report** buttons (Dashboard / Analytics) are inert
      (Timeline CSV export works) (§3). **[carried forward]**
- [ ] **Dashboard opt-in auto-refresh** not implemented (§10). **[carried forward]**
- [ ] **Chat image/file upload** — deliberately deferred (`claudeApi.uploadFile`) (§5).
      **[carried forward]**

## 🟡 Cross-cutting

- [ ] **Cross-device accent** — **[verified 2026-08-21]**. Accent and background persist
      to `localStorage` only (`shell/theme.tsx:60,63`), while light/dark — chosen on the
      *same* Appearance panel — persists to the backend via `configApi.setTheme`. Two
      adjacent controls behave differently and nothing says so. The precedent to copy is
      `/config/theme` plus the Mirror pattern (`CONTEXT.md:165`). Decide first whether
      appearance is **per-user** or **per-install**: `/config/theme` is install-wide
      config today, so on a shared deployment analysts may already be overwriting each
      other's light/dark choice.
- [ ] **Responsive / mobile-tablet** pass — layout targets desktop (§11).
      **[carried forward]**
- [ ] **Test coverage** — approvals approve/reject path and the detailed-feedback
      grading modal are uncovered (§13). **[carried forward]**

---

## ✅ Resolved since the 2026-06-22 pass

- **Canonical vs preview (§12)** — was the 🔴 lead decision this file said gated
  everything else. Settled by **#502**, which deleted the legacy MUI frontend: the
  Tailwind + CSS-vars console is canonical because it is the only UI. No retheme or
  migration plan is needed.
- **Auth-gating the console** — the console is wrapped in `ProtectedRoute` +
  `SetupGate` (`App.tsx`). `LoginScreen` is the single sign-in surface at `/login`;
  `/setup` sits deliberately outside `SetupGate` to avoid a redirect loop.
- **The `/redesign/*` routes are gone** — the console is served from `/`, each screen
  owning `/<screen>`. The back-compat redirects were dropped on 2026-08-21; the path
  now falls through to the in-shell 404.
- **`App.tsx` "illustrative mock data" comments** — removed; the console has been on
  real APIs since 2026-06-18.
- **Entity Graph "Preview the graph" CTA (§4)** — the CTA no longer exists, so there is
  no dangling handler. Only the stub above remains.
- **`SocConsole.test.tsx` is at 16 tests**, not the 13 recorded previously.

## ✅ Landed earlier (gaps doc may still read "open")

Routing + URL state (§1) · nav shell: collapse/brand/user-menu/permission-gating (§2) ·
filters / pagination / faceted search / New Case (§3/§4) · chat parity: CSRF+refresh,
model-from-config, history, cost band, reasoning traces (§5) · finding enrichment,
SLA-policy admin, agent builder, workflow run-detail (§8) · toast/snackbar, desktop
notifications, a11y baseline, error boundary (§10) · **Auto-Ops runtime** screen +
`InvestigationDetail` (§7) · **Login** screen, now auth-enforced (§7).
