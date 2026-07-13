# AI Decisions — Wiring Plan (redesign)

Approach doc for connecting the redesign **AI Decisions** screen to the real
backend, mirroring how `CasesScreen` was wired (`useCases.ts` + `mappers.ts`).
Written before implementation — see "Open decisions" before starting.

Cross-refs: `REDESIGN_GAPS.md` §6 (API surface), §8 (decision depth — "AI
Decisions tabs are dead"), §9 (data plumbing pattern).

---

## 1. Goal & scope

The redesign screen `screens/DecisionsScreen.tsx` renders entirely from the
static `DECISIONS` mock (`appData.ts:66`) with `decStats()` hardcoded
(`appData.ts:93`). All four tabs render the **same** list with hardcoded counts
(`DecisionsScreen.tsx:78-83`); switching tabs only clears selection. There is no
Analytics tab content, no Approvals queue, and the detail-pane review buttons are
inert (`DecisionsScreen.tsx:222-226`).

The old/current production page (`pages/AIDecisions.tsx`, 735 lines, route
`/ai-decisions`) is **two distinct features** behind one screen:

- **(a) Decisions analytics + feedback** — KPI strip, Pending / All / Analytics
  tabs, agent + status filters, and the `AIDecisionFeedback` modal
  (`components/ai/AIDecisionFeedback.tsx`, 395 lines).
- **(b) Pending Approvals queue** — a *separate* human-in-the-loop surface
  (`approvalsApi`) for workflow-phase + daemon actions, with `workflow_run_id`
  deep-links and a **mandatory rejection-reason** dialog.

This plan wires both onto the existing redesign shell + primitives.

---

## 2. API surface (already defined in `services/api.ts:75-139`)

### `aiDecisionsApi`
| fn | method | endpoint | use |
|----|--------|----------|-----|
| `getPendingFeedback(limit)` | GET | `/ai/decisions/pending-feedback?limit=` | **Pending** tab |
| `list({agent_id, has_feedback, limit, offset})` | GET | `/ai/decisions` | **All** tab + filters |
| `getStats({agent_id, days})` | GET | `/ai/decisions/stats` | **KPI strip** + **Analytics** tab |
| `submitFeedback(decisionId, {...})` | POST | `/ai/decisions/{id}/feedback` | feedback modal |

`submitFeedback` body: `human_reviewer` (required), `human_decision`
(`agree`/`partial`/`disagree`), `feedback_comment?`, `accuracy_grade?`,
`reasoning_grade?`, `action_appropriateness?` (each 0–1), `actual_outcome?`
(`true_positive`/`false_positive`/`true_negative`/`false_negative`/`unknown`),
`time_saved_minutes?`.

### `approvalsApi`
| fn | method | endpoint | use |
|----|--------|----------|-----|
| `listPending()` | GET | `/approvals/pending` | **Approvals** tab (`{data:{actions:[]}}`) |
| `approve(actionId, approved_by?)` | POST | `/approvals/{id}/approve` | Approve button |
| `reject(actionId, reason, rejected_by?)` | POST | `/approvals/{id}/reject` | Reject dialog (reason **required**) |

Both clients exist — **no `api.ts` change needed**. Reuse the shared axios
instance (cookie auth + CSRF + 401-refresh come for free, same as `useCases`).

---

## 3. Data model mapping (add to `mappers.ts`)

Add `ApiDecision` + `mapApiDecision()` alongside `mapApiCase`/`mapApiFinding`.
The backend returns snake_case + 0–1 confidence; the redesign view shape
(`Decision`, `appData.ts:51`) wants a percentage and display labels.

```
ApiDecision (backend → GET /ai/decisions)
  decision_id, agent_id, decision_type, confidence_score (0–1),
  reasoning, recommended_action, finding_id?, case_id?, workflow_id?,
  timestamp, human_decision? (agree|partial|disagree),
  decision_metadata? { investigation_id? }, actual_outcome?,
  time_saved_minutes?, has_feedback?
```

Mapping → redesign `Decision`:
| view field | source |
|------------|--------|
| `id` | `decision_id` |
| `agent` | `agent_id` via a `getAgentDisplayName` map (port from `AIDecisions.tsx:186-195`) |
| `type` | `decision_type` |
| `inv` | `workflow_id` ‖ `decision_metadata.investigation_id` ‖ `finding_id` ‖ `case_id` ‖ `—` |
| `conf` | `round(confidence_score * 100)` |
| `ai` | `recommended_action` |
| `human` | label from `human_decision` (`agree`→"Approved", `partial`→"Modified", `disagree`→"Rejected", none→"Pending") |
| `outcome` | `human_decision`→`Outcome`: `agree`→`agree`, `disagree`→`disagree`, `partial`→`modify`, none→`pending` |
| `saved` | `time_saved_minutes` → `"25m"` / `"2h"`, else `—` |
| `time` | `fmt(timestamp,'MMM d, HH:mm')` (reuse `mappers.ts` `fmt`) |
| `rationale` | `reasoning` |
| `evidence` | ⚠️ **backend has no evidence array** — old page never showed one. Default `[]` and hide the card when empty (see Open decisions). |

> Note: redesign `Decision.outcome` collapses `human_decision` (agree/partial/
> disagree) — it is **not** the backend's `actual_outcome` (true/false-positive).
> Those are different axes. The old page shows `actual_outcome` only inside the
> Analytics outcome-distribution chart; keep that distinction.

---

## 4. Data hooks (new `useDecisions.ts`, mirror `useCases.ts`)

Same `useEffect` + local-state + `Phase` (`loading|ready|error`) + `reload()`
pattern — no React-Query (consistent with the rest, `REDESIGN_GAPS.md §9`).

- `useDecisions({ agentId, status })` → All tab. Calls `list()`, maps,
  exposes `{rows, phase, error, reload}`. Re-fetches on filter change.
- `usePendingDecisions()` → Pending tab. `getPendingFeedback(50)`.
- `useDecisionStats({ agentId, days })` → KPI strip + Analytics tab. `getStats()`.
- `usePendingApprovals()` → Approvals tab. `approvalsApi.listPending()`,
  reads `res.data.actions`, exposes `reload` for post-action refresh.

(Could be one hook with sub-fetches like `useCaseDetail`, but four small hooks
keep each tab's loading/error state independent — preferred.)

---

## 5. Tabs — what each needs (currently all dead, `DecisionsScreen.tsx:78-95`)

| tab | data | UI work |
|-----|------|---------|
| **Pending** | `usePendingDecisions` | existing table, real rows + live count |
| **All** | `useDecisions(filters)` | wire the two `.chip` filters (agent / status) — currently static spans (`:97-98`) — + the search input (`:100`) |
| **Analytics** | `useDecisionStats` | **new content** — outcome distribution (bar/`Spark`/`Donut` from `charts.tsx`) + perf metrics (decisions/day, avg time saved, needing-review). Port from `AIDecisions.tsx:640-688`. |
| **Approvals** | `usePendingApprovals` | **new table** — Action / Target+Run / Phase / Reason / Created / Approve+Reject. `workflow_run_id` deep-link. Port from `AIDecisions.tsx:501-638`. |

KPI strip (`DecKpis`, `:36-67`): swap `decStats()` → `useDecisionStats` →
`total_decisions`, `feedback_rate`, `agreement_rate`, `total_time_saved_hours`.

---

## 6. Modals (the focus) — 2 dialogs + 1 inline pane

The old page uses MUI `Dialog`. The redesign has its own modal primitive
**`Popup`** (`ui.tsx:20-76`, scoped `.modal-overlay/.modal`, Esc + focus-return +
outside-click) and form primitives **`Field` / `TextInput` / `Toggle` / `Select`**
(`ui.tsx:273-379`). Use those — **do not** import MUI into `redesign/`
(`REDESIGN_GAPS.md` keeps the bundle MUI-free; embedding app components risks the
context-isolation crashes noted in memory).

### Modal A — Reject Action dialog (HARD requirement)
Old: `AIDecisions.tsx:699-732`. Triggered by Reject in the Approvals table.
- `Popup title="Reject action" width≈520`.
- Action title (read-only) + **required** multiline reason `TextInput`/textarea.
- helper: "Required. Recorded on the workflow run's audit trail."
- Cancel + **Reject** (`.btn.danger`), Reject disabled until reason non-empty /
  while a request is in flight (`approvalActing` guard, `:721-731`).
- On submit → `approvalsApi.reject(id, reason)` → close → `reload()` approvals.
- Approve has no dialog: `approve(id)` directly, disable row while acting.

### Modal B — Decision feedback (the rich one)
Old: entire `AIDecisionFeedback.tsx`. Fields:
1. Reviewer name (required)
2. Agree / Partially agree / Disagree (→ `human_decision`)
3. Accuracy grade (1–5 stars → 0–1)
4. Reasoning grade (1–5 stars → 0–1)
5. Action-appropriateness grade (1–5 stars → 0–1)
6. Actual outcome (TP/FP/TN/FN/Unknown radio → `actual_outcome`)
7. Time saved slider (0–120 min → `time_saved_minutes`)
8. Comments (multiline)

→ `submitFeedback(decision_id, {...})` (scale stars /5, omit Unknown outcome &
0-min as the old form does, `AIDecisionFeedback.tsx:84-110`).

⚠️ The redesign has **no Rating (stars) or Slider primitive**.

**DECISION (resolved): Hybrid.** The inline "Your review" pane stays the quick
path; the rich fields move into an expandable `Popup`:
- Inline (detail view, already laid out at `DecisionsScreen.tsx:218-228`):
  Approve / Modify / Reject + comment box → submits `human_reviewer`,
  `human_decision`, `feedback_comment` only.
- "+ Add detailed feedback ▸" expander opens a `Popup` carrying fields 3–7
  (accuracy / reasoning / action grades, actual outcome, time saved) →
  submits the full body.
- Build small `Rating` (stars) + `Slider` primitives in `ui.tsx` for the Popup.
- `Modify` = `human_decision: 'partial'`.

### Inline review pane (detail view)
Already laid out (`DecisionsScreen.tsx:218-228`) but inert. Wire the
Approve/Modify/Reject buttons to `submitFeedback` with the matching
`human_decision` (+ comment) and refresh. The detailed-feedback Popup reuses the
same `submitFeedback` call with the extra graded fields.

---

## 7. Deep-links & cross-nav

Old page deep-links out: Investigation → `/orchestrator?highlight=`, workflow run
→ `/workflows?run_id=` (`AIDecisions.tsx:444,550-554`). The redesign shell has
**no router / URL state** (`REDESIGN_GAPS.md §1`), so these become in-shell
`go('workflows')` navigations (or no-ops for now). The inbound deep-link the old
page accepts (`?agent_id=`, `?investigation_id=`) also has nowhere to land until
routing exists — note as deferred, don't block on it.

---

## 8. Files to change

- `mappers.ts` — add `ApiDecision`, `mapApiDecision`, `getAgentDisplayName`,
  agent-id labels, time-saved formatter.
- `useDecisions.ts` — **new** — the four hooks (§4).
- `screens/DecisionsScreen.tsx` — replace mock imports with hooks; build
  Analytics + Approvals tab bodies; wire filters/search; wire review pane +
  modals; add loading/error/empty states per `phase` (mirror `CasesScreen`).
- `ui.tsx` — add `Rating` (stars) + `Slider` primitives for the detailed-feedback
  `Popup` (Hybrid decision, §6).
- `appData.ts` — leave `Decision`/`Outcome` types (now fed by the mapper); the
  `DECISIONS`/`decStats` mock constants can stay as a fallback or be removed.
- No `api.ts` change. No `data.ts` change (NAV/TITLES/ScreenKey already have
  `decisions`).

---

## 9. Decisions (resolved)

1. **Feedback fidelity → Hybrid.** Inline Approve/Modify/Reject + comment for the
   quick path; "+ Add detailed feedback" `Popup` for grades/outcome/time-saved.
   Build `Rating` + `Slider` primitives. (See §6.)
2. **Evidence card → hide when empty.** Backend returns no evidence list (mock-only
   embellishment); render the "Supporting evidence" card only when present.
3. **Approvals deep-links → plain text + tooltip** until §1 routing lands (no
   router to land a specific `workflow_run_id`).

---

## 10. Out of scope (this pass)

URL routing / deep-link landing (§1), the per-finding/case detail depth (§8),
SLA, react-router adoption. This pass = data + the two queues + modals only.
