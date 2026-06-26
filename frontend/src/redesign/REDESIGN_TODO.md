# SOC Console redesign — TODO (what's actually left)

At-a-glance checklist of remaining work, reconciled against the code on
2026-06-22. The detailed audit history lives in [`REDESIGN_GAPS.md`](./REDESIGN_GAPS.md);
this file is the current "what's left" view. Section tags (§) reference the gaps doc.

> **No dead files.** Every `.ts`/`.tsx` under `src/redesign/` is wired —
> including `shell/Loader.tsx` (Suspense fallback in `App.tsx`) and
> `screens/login/LoginScreen.tsx` (routed at `/redesign/login`). Nothing to delete.

---

## 🔴 Lead decision — gates everything below (§12)
- [ ] **Canonical vs preview.** Decide whether the Tailwind + CSS-vars shell
      becomes canonical (retheme MUI globally) or stays a preview route. No
      migration/rollout plan exists for the canonical path.
- [ ] **Auth-gate `/redesign`.** `/redesign/:screen` is still a **public route**
      (not under `ProtectedRoute` — `App.tsx:54-57`). `LoginScreen` is built and
      routed at `/redesign/login` but the route tree doesn't enforce a session.
- [ ] **Cross-device accent.** Accent persists to `localStorage` only; no backend
      field (the legacy app's accent is fixed cyan).

## 🔴 Net-new surfaces still missing (§7)
- [ ] **VStrike / CloudCurrent 3D control plane** — kill-chain replay, storyline
      playback, camera control (`vstrikeApi`). Today: Entity Graph "coming soon" stub.
- [ ] **CostAnalytics standalone page** — only `CostAnalyticsCard` exists in
      Settings; the full page (KPI cards, cost-by-agent / tokens-by-model charts,
      pricing provenance, admin recalculate) is not ported (`pages/CostAnalytics.tsx`).
- [ ] **Timesketch sketch-management page** — case export is wired; sketch
      list/create/open + local Docker-stack lifecycle is not.
- [ ] **Outbound webhooks** (`webhooksApi`) — not represented.
- [ ] **Full Investigation workspace** — Auto-Ops `InvestigationDetail` landed, but
      the synchronized `EventTimeline` + `EntityVisualization` with bi-directional
      cross-highlighting and VStrike iframe pivots is still partial (verify depth).

## 🟡 Per-screen depth (§8)
- [ ] Per-case **relationships / evidence / audit-log**, structured close, escalate,
      bulkUpdate (confirm against the legacy case set).
- [ ] Finding **VStrike `NetworkContextPanel`** — needs the VStrike provider the
      standalone `/redesign` shell doesn't mount.
- [ ] **Versioned `WorkflowBuilder`** phase model (ordered phases → agent + tool set,
      per-phase approval gates, versioning, run history). Current builder is partial.

## 🟡 Smaller gaps in already-wired screens
- [ ] **Export / Generate-report** buttons (Dashboard / Analytics) are inert
      (Timeline CSV export works) (§3).
- [ ] **Entity Graph "Preview the graph" CTA** has no handler (ties to VStrike) (§4).
- [ ] **Dashboard opt-in auto-refresh** not implemented (§10).
- [ ] **Chat image/file upload** — deliberately deferred (`claudeApi.uploadFile`) (§5).

## 🟡 Cross-cutting
- [ ] **Responsive / mobile-tablet** pass — layout targets desktop (§11).
- [ ] **Test coverage** — approvals approve/reject path + the detailed-feedback
      grading modal are uncovered (§13). `SocConsole.test.tsx` is at 13 tests.

---

## ✅ Recently landed (gaps doc still reads "open" for some of these)
Routing + URL state (§1) · nav shell: collapse/brand/user-menu/permission-gating
(§2) · filters / pagination / faceted search / New Case (§3/§4) · chat parity:
CSRF+refresh, model-from-config, history, cost band, reasoning traces (§5) ·
finding enrichment, SLA-policy admin, agent builder, workflow run-detail (§8) ·
toast/snackbar, desktop notifications, a11y baseline, error boundary (§10) ·
**Auto-Ops runtime** screen + InvestigationDetail (§7) · **Login** screen built +
routed at `/redesign/login` (§7, not yet auth-enforced — see lead decision).

## 🧹 Housekeeping
- [ ] `REDESIGN_GAPS.md` §7 still says Auto-Ops / Login are "not represented" — stale.
- [ ] `App.tsx:27,50` comments still call the redesign "illustrative mock data" —
      it's been on real APIs since 2026-06-18.
