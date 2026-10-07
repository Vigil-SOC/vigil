# Vigil Console, phase 1 PRD

Rev 1.1-P1 · 2026-09-30 · derived from PRD rev 1.1 (Mayank Kumar, 09/29/2026) and the v7 design package, rescoped per the team's phase-1 message of 09/30/2026.

## How to read this

This document is the scope of record for phase 1. Where it and PRD rev 1.1 disagree, this document wins for phase 1; rev 1.1 remains the description of the full product and nothing here retracts it. Where this document and a board disagree on appearance, the board wins. Where they disagree on behaviour or on what is in scope, this document wins.

Phase 1 is a trailer of the full design: every phase-1 page looks like its board and the parts of the board that need phase-2 data are present as labelled placeholders, not silently missing. Phase 1 runs on today's data model. It adds a short list of backend endpoints and fields (section 4). It does not add the ledger write path, the autonomy controller, the Act contract, Tell/Do, trust scoring, agreement or graduation. Those are phase 2 and are listed in section 15 so nobody builds them by accident.

Section 16 records every decision this phase needed; there are no open decisions in this document. The design reference for phase 1 lives beside this document in the Vigil repository at `docs/design/console/`: `README.md` (start there; it maps boards to PRD sections and lists what is left out on purpose), `boards/png/` (renders of every phase-1 board in both themes), `boards/*.dc.html` (interactive originals), `components/`, `tokens/` and the phase-1 edition of `DESIGN.md`. The full v7 package and PRD rev 1.1 are kept outside the repository.

## 1. Scope

**In phase 1, as pages:** Onboarding (wizard, first run, tour, demo data), Home (board `MainP2`), Overview, Triage queue, Cases (list and case page), Ask Vigil, Agents & workflows, Settings.

**In phase 1, as shell:** header, navigation, autonomy chip, command bar with a fixed set of built-in commands, Ask Vigil dock, profile menu, system status line, case drawer, toasts with undo where the action is reversible.

**Phase 2, not in this document except as placeholders:** Pulse, Fleet Wall, Coverage, Tool permissions, the Needs you island, custom and role-gated commands, multi-user (Team and access, invites, View as, watchers), closed-case review, incidents, Tell and Do, revert, per-case tier, trust strip, hold with reason and duration, fleet-wide directions, on-demand work.

**Not renamed in code.** On-screen vocabulary follows rev 1.1 (alert, case, explanation, evidence row, Checked, blind spot, Needs you, Good/Fair/Poor). Internal names (Finding, intake_triggers, investigations, hypothesis, approval_actions, agent_events, case_audit_logs) stay as they are. Phase 1 changes what the console calls things, not what the code calls them.

## 2. Placeholder classes

Every element on a phase-1 board that phase 1 cannot populate is handled by exactly one of these rules. Each page section below says which rule applies to which element.

| Class | Rule | Example |
|---|---|---|
| **Not measured yet** | A number or panel whose data Vigil does not record. Shown in place, in the board's layout, with the words "Not measured yet" and an ⓘ that says what will measure it. Never estimated in the UI. | Overview outcome nodes for paused, stuck and incidents |
| **Later** | A control, tab or mode the board shows in a primary position. Shown disabled with the tooltip "Coming in a later release". Never wired to a no-op that looks like it worked. | Tell and Do modes on the case composer; Rescue on the Triage queue |
| **Omitted** | Anything else phase 2. Not rendered, no nav item, no dead space. | Fleet Wall tab, per-case tier control, hold with duration |

Good, Fair and Poor are shown as words wherever a value has a limit from configuration. Where there is no limit, the value is shown without a level word. Every widget with a number carries an ⓘ stating source and calculation; where the calculation is trivial the ⓘ says so.

## 3. Principles for phase 1

1. Boards win on appearance. Both themes complete. Root minimum width 1440 px.
2. Numbers come from the API. No policy number (limit, preset, threshold, cap) is hard-coded in the UI. Where today's UI holds one, it moves behind an endpoint in phase 1 (section 4).
3. Every human action already available today keeps working through its existing endpoint, restyled.
4. A placeholder is honest. It says what is missing and why. Nothing on screen claims to do what it does not.
5. One shell, all pages. The case drawer, the command bar, the dock and the status line are shell components that every page opens the same way.
6. Polling is fine. Live push is phase 2.
7. Confirmation classes from rev 1.1 A6 apply to the actions that exist: press-and-hold 1.6 s for anything that cannot be undone (today: response actions such as isolate and block through approvals), one tap plus an 8 s undo fuse for everything reversible. Hold-to-confirm is never used on a reversible action.

## 4. Backend changes in phase 1

The rule for inclusion: a small addition over data Vigil already holds, that more than one phase-1 page consumes or that removes a policy number from the UI. Each is one to three factory PRs.

| # | Change | Why it earns its place | Consumers |
|---|---|---|---|
| B1 | **One read endpoint for "what needs a person"** that unions today's pending approvals, open run checkpoints and any intake trigger waiting on a person into one shape: kind, case or alert, ask text, created, expires (where the source carries one), reversibility, source object id. Ranking is by expiry where present, else age, oldest first. | Home, Cases (Needs you flag and sort), case page decision block, nav badge, and the phase-2 island later | Home, Cases, case page, shell |
| B2 | **Overview aggregates**: arrivals today per source; outcome counts computable today (closed by agent alone from `closed_by_kind`, closed by person, needs a person from B1); agents running now per workflow with running count and 30-day completion rate with sample size. Everything else on the diagram is Not measured yet. | Overview is the demo frame and today's UI would have to fetch hundreds of runs to compute this in the browser | Overview |
| B3 | **Triage queue read**: intake triggers with kind, source, arrival, state, grouped-into case where launched or merged, workflow assigned, and the existing ranking inputs (severity, TTL, age) as a breakdown; per-source arrival count and ingestion lag from collection cursors. Read-only. | The queue is inert in phase 1 but has to show real rows | Triage queue, Overview strip |
| B4 | **Combined system status**: one endpoint folding health, provider routability, federation health, MCP server status and storage into one level and one line. | The status line sits above everything on every page | Shell, Home, Onboarding checks |
| B5 | **Source console link template** on integration descriptors, so an alert row can offer "Open in source". | Cheap descriptor field; Triage, Overview feed and case Evidence all use it | Triage, Overview, case page |
| B6 | **Noise label on an alert** (`Mark as noise`): stored as a label on the finding with actor and time; it is collected for phase 2 triage learning and hides the alert from the Overview feed. It does not change scoring in phase 1 and the ⓘ says so. | The one teaching action on the Overview feed | Overview |
| B7 | **Demo data flag** on findings and cases created under demo mode, honoured by memory distil so demo never enters memory, plus "clear demo data" in one call. | Onboarding promises demo is labelled and clearable | Onboarding, every surface's Demo badge |
| B8 | **Setup steps endpoint** behind Home's "Get more from Vigil": the fixed ranked list (connect more tools, notification route, detection rules linked, model per agent, custom skill) with done/open per step, computed server-side from existing config. | Home's main phase-1 content; also the first-run checklist | Home, first run |
| B9 | **Autonomy presets served, not hard-coded**: the Conservative, Balanced and Broad profile values and the default case limits move from `AutoInvestigateSection.tsx` to the orchestrator config endpoint. | Principle 2 | Settings › Limits and autonomy, Onboarding |
| B10 | **Chat conversation search and page context**: search over the user's private conversations; two nullable columns on `conversations`, `case_id` and `page_context`, set when the dock or the case composer opens a conversation. | Ask Vigil dock history and "Using Overview" | Ask Vigil |
| B11 | **Case page projections for lead runs**: tool calls with question, result size, cost and latency (latency journaled where missing); replay available for `investigate` runs as it is for hunts. | The Checked tab and Watch a run would otherwise be hunt-only | Case page, Agents & workflows |
| B12 | **Agent model fallback** field on custom agents, journaled with actor. | Agent editor and Settings › AI models show it | Agents & workflows, Settings |

**Explicitly not in phase 1 backend:** unified case record or write path; cases moving onto the hypothesis loop; Assist/Act tiers stored or enforced; stop-reason taxonomy; expiry defaults; revert windows or revert execution; Tell/Do parsing or re-plan; trust scores, floors or drop line; agreement or graduation; hold model; incident status; commands registry; roles changes; retention; live push. Section 15 lists where each goes.

## 5. Shell

Board: every app board; `Search.dc.html` for the command bar.

- **SH-P1.** Header 52 px: logo, command bar (⌘K), autonomy chip, avatar. Navigation 46 px: Home, Overview, Triage queue, Cases, Agents & workflows, Settings, then a "More" menu. Home and Cases carry a Needs you count badge from B1.
- **SH-P2. Needs you island: Omitted.** The header shows no island. The Home Needs you badge and section carry the count.
- **SH-P3. Autonomy chip** shows "Assist · asks before changes" or "Act · reversible changes on its own", derived from today's orchestrator configuration (manual approval forced or auto-response off reads as Assist; otherwise Act). It opens Settings › Limits and autonomy. Nothing new is stored. The ⓘ states the derivation.
- **SH-P4. Command bar.** ⌘K from anywhere. Three behaviours: find, ask, run. Results carry a destination tag (opens a case, on a board, sends to Ask Vigil, runs a workflow). Enter follows the highlighted result; Tab hands the text to Ask Vigil; Esc closes. Ranking: exact case and alert ids first, then cases, then boards, then commands, then recent. Search uses today's case search and findings endpoints. Recent searches are kept per person in the browser.
- **SH-P5. Built-in commands, fixed set, no registry, no role gating.** Typing `/` lists them with an argument hint. A chosen command shows what it will run and on what, then Run. Reversible commands carry the undo fuse where the underlying action can be undone; otherwise the preview is the confirmation.
  - `/investigate <alert id | host | user>` starts the investigation workflow through today's workflow execute endpoint.
  - `/hunt <hypothesis>` starts a hunt run.
  - `/replay <case>` opens the case in replay.
  - `/ask <question>` opens Ask Vigil with the page as context.
  - `/ticket <case>` creates a Jira ticket from the case using today's export.
  - `/hold`, `/isolate`, `/phish` and custom commands: **Later** in the list, disabled.
- **SH-P6. Profile menu:** name and role; Setup guide with count left; Take the tour; Share feedback (opens the GitHub new-issue link per D3; no Settings page for it in phase 1); About Vigil; theme switch; Settings; Sign out. View as: **Omitted**.
- **SH-P7. System status line** from B4 sits above the page content and turns red when the level is Poor.
- **SH-P8. Ask Vigil dock** at 400 px on any page off a case (section 11). On the case page the composer replaces the dock.
- **SH-P9. Case drawer** is a shell component opened by Home, Triage queue, Cases, the command bar and Overview, at 75% width over a scrim, expandable to full page. It holds the case page (section 10).
- **SH-P10. "More" menu** hosts the current screens that have no phase-1 page: Analytics, Case metrics, AutoOps, AI decisions, Health. They receive the new tokens and shell and no further work (D8). Phase 2 retires them into Pulse, Fleet Wall and Coverage.
- **SH-P11. Toasts** for every committed action; undo fuse where reversible.
- **SH-P12. Extension screens** mount under More.

## 6. Onboarding

Boards: `OnboardingFlow`, `Onboarding`, `OnboardingConnect`, `OnboardingModel`, `OnboardingAgents`, `OnboardingLimits`, `OnboardingDone`, `FirstRun`, `Tour`, `TourIsland`, `TourAsk`.

- **ON-P1. Wizard, five steps** (invites removed): system checks (B4 plus storage and provider checks, run step by step with status marks); connect your data (today's integration and federation setup forms, with a test); where AI runs (provider selection from today's providers, showing hosted or local and whether data leaves the site); agents and workflows (enable built-in workflows); limits and autonomy (profile from B9, default case limits, and the tier as two cards with Act recommended: the choice sets today's orchestrator auto-response and approval settings, which is what the chip reads); summary. Every choice is saved through today's config endpoints, which already journal to the config audit log.
- **ON-P2. First-run Home** shows the setup checklist from B8 as a tick-off list, and Overview shows connect-data placeholders. The Needs you section shows the empty state.
- **ON-P3. Tour, three stops:** the pages, the Needs you section on Home (in place of the island stop), Ask Vigil. Skippable; shown once; "Take the tour" in the profile menu replays it.
- **ON-P4. Demo data** through today's demo mode with B7: a Demo badge on every surface that shows demo rows, one-click clear, never enters memory.

## 7. Home

Board: `MainP2`. Job: is anything waiting on me, and what should I set up next.

- **HM-P1. Headline** states the count from B1 ("4 decisions wait on you. Everything else is running." or "Board clear.").
- **HM-P2. Command bar** inline, as SH-P4 and SH-P5. Suggestion chips show the built-in commands and a "picked up automatically today" figure from B3 (launched over arrived).
- **HM-P3. Get more from Vigil**: the five steps from B8, fixed order, top open steps shown, each with one primary action that opens the relevant Settings page and a "Not now" that hides it for the session. "A step cited by a real case jumps to the front": **Omitted** (needs blind spots).
- **HM-P4. Needs your attention**: cards from B1, soonest expiry first where an expiry exists, else oldest first. Each card shows the ask as verb and object, the case or alert, when it expires or how long it has waited, what kind of thing it is (approval, checkpoint, alert waiting), and actions. Approvals and checkpoints use today's approve and reject endpoints with the confirmation class from principle 7. "Open case" opens the drawer. Top four shown; the rest as "N more waiting" opening the full list. Fields the card schema in rev 1.1 asks for and today's objects do not carry (stop reason with limit link, default on expiry, sources agree and disagree, seen before, incident proposals): **Omitted** from the card, not shown blank.
- **HM-P5. Fleet tiles: Omitted** (return with Pulse).
- **HM-P6. Empty state** per the board: no alerts yet, connect data, explore with demo data, take the tour.

## 8. Overview

Boards: `Overview`, `OverviewAlert`, `OverviewEmpty`, `OverviewFull`.

- **OV-P1. Flow diagram**: sources with vendor logos and today's arrivals (B2); engine node; outcome nodes. Populated from B2: closed by agent alone, closed by a person, needs a person. **Not measured yet**: paused, stuck, incidents. Every populated count opens Cases or the Triage queue filtered.
- **OV-P2. Agents running now**: one row per workflow with running count, 30-day completion rate with sample size, and a level word (Good at or above 95%, Fair 85 to 95%, Poor under 85%). "What each agent is doing" comes from the run's current phase where a run is live.
- **OV-P3. Live alert feed**, latest 50, kind, source, severity, state; polled. Opening an alert shows source evidence with "Open in source" from B5 and the actions Launch workflow (today's execute), Mark as noise (B6), and Create ticket when the alert has a case (today's Jira export). ServiceNow: **Later**.
- **OV-P4. Full-screen mode** with no top bar; empty state with connect-data placeholders.

## 9. Triage queue

Board: `Triage`. Present and inert: the queue shows real rows and the controls that would teach or rescue are Later.

- **TQ-P1. Live column** from B3: kind, source with logo, arrival, time to pick-up, state mapped to the board's words (waiting, picked up, formed new case, grouped into forming case, attached as evidence, expired), grouped-into with the case as a door, workflow assigned. Filters by kind, source and state. A row opens source evidence with "Open in source" (B5); the case door opens the drawer.
- **TQ-P2. Ranking breakdown on expand**: the inputs today's ranking uses (severity, time to live, age), labelled as such. Score against a floor, trust and weight: **Not measured yet** inside the same panel.
- **TQ-P3. Rescue: Later.**
- **TQ-P4. Status strip**: picked up automatically (share; goal Omitted), waiting in line (count; drop line Omitted), cases created today, per-source arrivals and lag from B3. Sources below floor: **Not measured yet**.
- **TQ-P5. Tier assigned** column: Omitted.

## 10. Cases

Boards: `Cases`, `CaseDetail`, `CaseInvestigation`, `CaseComments`.

### Cases list

- **CS-P1. Table** over today's cases with the columns the data carries: id, title with description on hover, workflow, state (case status combined with the orchestrator investigation status where one exists), Needs you flag from B1, SLA time left with level word from today's SLA endpoint, severity, alerts combined (linked findings count), steps (iteration count), limit used (cost against the investigation's cost cap; nearest edge is "budget" only), owner (today's assignee), comments count, last activity, age. Columns rev 1.1 asks for and today's data does not carry (tier, waiting-on, held-by, steps since explanation moved, memory warm or cold, origin, reopen count, incident status, watchers): **Omitted**.
- **CS-P2. Default sort**: Needs you first, then SLA time left, then last activity. Filters on state, workflow, severity, source, SLA at risk, owner, closed.
- **CS-P3. New case** through today's create. "Review closed cases": **Omitted**.
- **CS-P4. Multi-select with Hold, Release, Assign to me: Later.**
- **CS-P5. Strip**: open by state, SLA at risk, closed today with the share closed by an agent alone (from `closed_by_kind`). Oldest pause and hold: Omitted.

### Case page

One layout, opened as a drawer or full page (SH-P9). Phase 1 shows the layout and mood the case's data supports; the moods and blocks that need the controller are placeholders.

- **CP-P1. Header**: breadcrumb, expand, close; title, severity, state pill with today's status, workflow, alerts combined, opened; SLA resolve-by. Tier control: **Omitted**. Trust strip: **Not measured yet**, as one line in the header position.
- **CP-P2. Tab strip with counts**: Summary, Explanations, Evidence, Checked, Memory and blind spots, Record. Counts come from the tab's own data.
- **CP-P3. Summary, first block by what the data supports.**
  - *Needs you*: when B1 has an item for this case, the decision block shows the ask, the case's alerts, what the approval or checkpoint carries (reversibility, reason text, proposed action), and the actions Approve (press-and-hold when not reversible) and Reject with a free-text reason through today's endpoints. Approve narrower, Hold with reason and duration, "if you do nothing", the four counted tiles and strongest for and against: **Omitted**.
  - *Running*: "Now · phase N" from the live run with the current tool; findings and decisions so far. "What it changed" and "Planned next": **Later**, rendered per D5 as a heading, one explanatory line and an ⓘ, disabled.
  - *Closed*: verdict and closure category, who closed it and whether an agent closed it alone, the strongest findings. "Your assessment" (Agree/Disagree): **Later**.
  - A slim red Needs you strip under the tabs on every non-Summary tab while a B1 item is open for the case.
- **CP-P4. Summary, then**: "Agents on this case, right now" from live runs, and door tiles into the audit tabs with their counts. "What your team told the agents": Omitted.
- **CP-P5. Explanations tab**: for cases whose run is a hunt, hypotheses with status, evidence for and against counted, added by, and the note on why it changed, using the hunt projection's statuses mapped to the board's words. For cases whose run is an investigation (lead loop), the tab shows "This workflow does not test explanations yet" with an ⓘ. Add an explanation and Rule one out: **Later**.
- **CP-P6. Evidence tab**: hunt evidence rows with step, observation, source with logo, source tier, stance, bears on, collected by; for investigation runs, the run's findings in the same table. Row actions (Expected, Unreliable, Already handled): **Later**.
- **CP-P7. Checked tab**: every tool call from the run with question, tool, result size, cost and latency (B11). Questions with no source appear from the run's visibility gaps.
- **CP-P8. Memory and blind spots tab**: recall at start and mid-run from the run's recall events; declared gaps from the episodic gap store and visibility gaps from the run. Withdraw and record-a-blind-spot: **Later**.
- **CP-P9. Record tab**: one list, newest first, merging the run's agent events and the case's audit rows, typed as agent, human, memory, system by source; filter chips by type. Signed and hash-chained is stated only for the rows that are (the run's events); the ⓘ explains the two sources. "Why?" on a row opens the composer in Ask with the row attached. Replay opens today's run replay for the case's run. Verify chain and Export audit use today's verify and export.
- **CP-P10. Details panel** (280 px): workflow, limit used (budget only) with level word, resolve-by, entities, cost, People collapsed (owner, comments, tasks, linked tickets from today's endpoints). "Known about these entities" from the entity recall where present. Watchers and Assign to me: **Omitted** (no multi-user).
- **CP-P11. Composer** pinned to the bottom of every tab: Ask mode over today's chat with the case attached as context, with the thread above; Tell and Do modes: **Later**. Citation chips open the Evidence tab at the row when the answer cites a row id; otherwise the answer is plain. The note reads "Private to you · Ask only" (D4); the conversation is the user's and carries the case id so phase 2 can promote it to the case record.
- **CP-P12. Reopen** through today's reopen, which withdraws the verdict.
- **CP-P13. Incident declaration**: Omitted.

## 11. Ask Vigil

Boards: `PulseChat` (layout only; Pulse is phase 2), `ChatHistory`, `TourAsk`.

- **AV-P1.** Off a case, the dock at 400 px on any page, marked private to the user, with searchable history grouped by day (B10), the page context shown ("Using Overview"), @ to attach a case (B10), / to list the built-in commands. Streaming and Markdown as today.
- **AV-P2.** No per-conversation model picker; the model comes from Settings › AI models. Today's picker, system prompt field and cost meter are removed from the dock.
- **AV-P3.** Fleet-wide directions, on-demand work with cost, and the trace-as-model-text labelling beyond today's thinking display: **Omitted**.
- **AV-P4.** Tab from the command bar hands the typed text to the dock, or to the case composer when a case is open.

## 12. Agents & workflows

Boards: `Agents`, `AgentEditor`, `SkillEditor`, `WorkflowRun`. Fastest path: today's implementation restyled onto the boards.

- **AW-P1. Workflows table**: name, agents, runs today, cost per run, last edited, for built-in and custom workflows from today's catalog. Tier, trust and agreement, usage signals: **Not measured yet** as columns collapsed into one "Trust" column showing the placeholder.
- **AW-P2. Builder**: today's graph builder and run modal, restyled. The loop-shaped editor from the board: phase 2. "Generate with AI" keeps today's generate endpoint and labels the result as an AI draft until saved. "Test on a sample alert" uses today's preflight.
- **AW-P3. Agent editor**: model, thinking, max tokens, tools it may call, from today's custom-agent endpoints, plus fallback (B12). Built-in agents are read-only in phase 1.
- **AW-P4. Skills** shown as folders over today's read-only skill library, with the workflows that use each. Import and edit: **Later**.
- **AW-P5. Tool permissions: Omitted** (no page, no nav entry).
- **AW-P6. Commands**: a read-only table of the built-in set from SH-P5 with name, what it runs and arguments. Create custom, who may use it, runs count: **Later**.
- **AW-P7. Watch a run**: the step player over today's run replay for hunt and, with B11, investigation runs: what the agent was shown, its action, tool, cost, and where it stopped; the model trace per step where the reasoning trace exists, labelled as model text.

## 13. Settings

Boards: `Settings`, `SettingsIntegrations`, `SettingsCustom`, `SettingsCollection`, `SettingsSLA`, `SettingsLimits`, `SettingsData`. Seven screens; Team and access and Feedback are phase 2.

- **SE-P1. AI models**: per-agent model with fallback (B12), provider and hosted-or-local, gateway budget as today; caching, context and tool-result size where today's config carries them. Changes confirm and journal through today's config audit.
- **SE-P2. Integrations**: today's list and setup wizard restyled, with status, last successful read, and whether a source-console link template is present (B5). Vigil's own MCP credentials as today, with hold-to-revoke. Custom integration builder as today under its own screen.
- **SE-P3. Alert collection**: federation sources with cursor, lag and expected interval (B3), poll now; rule sources; Kafka and ingestion under Data and uploads.
- **SE-P4. SLA policies**: today's policies screen restyled; escalation steps as today's escalation rules.
- **SE-P5. Limits and autonomy**: fleet tier as two cards (writes today's auto-response and approval settings, as ON-P1), auto-investigation on, off, dry run; profiles from B9 with their values shown; default case limits (budget, steps, idle); concurrency and hourly cost caps as today. Scrub fields and the duration picker per the board; loosening a limit confirms. Attention cap, unattended window, per-tool-class defaults and phase flags: **Omitted**.
- **SE-P6. Data and uploads**: file, S3 and Kafka ingestion and detection rules as today; demo data clear (B7). Retention: **Not measured yet** as a read-only panel stating current behaviour.
- **SE-P7. Sections with no board** (Services, System, General, Developer, Appearance): Per D7: Appearance is removed (theme lives in the profile menu); the other four are kept as one "System" tab at the end of Settings with the new tokens and no further work.
- **SE-P8. Team and access, Feedback: Omitted.** Roles, users and sign-in stay as they are today under the System tab.

## 14. Definition of done, phase 1

A phase-1 requirement is done when:

1. The screen matches its board in both themes at 1440 px, with the placeholder classes applied exactly as this document says. A reviewer compares a screenshot to the board.
2. Every number on screen comes from an API response or a configuration value served by the API. No preset, limit or threshold is written in the UI.
3. Every action that exists today still works through its existing endpoint, with the confirmation class from principle 7.
4. Every placeholder is labelled per section 2 and its ⓘ says what will fill it.
5. The screen has tests for its data mapping and its states (empty, loading, populated, error, demo).
6. Nothing phase 2 has a nav entry, a route or a fake result.

Rev 1.1's L test (fold from record events, byte-identical replay) is not applied in phase 1.

## 15. Phase 2 map

Where each deferred item goes, so phase 1 leaves the right seams.

| Deferred | Phase-1 seam it depends on | Phase 2 item |
|---|---|---|
| Needs you island, tray, hover | B1 shape | island over B1 once the ask object carries stop reason and expiry |
| Decision card fields (stop reason with limit link, default on expiry, counted stances, seen before) | B1 | controller stop reasons and expiry policy |
| Incident proposals and declaration | none | incident status on cases |
| Per-case tier, Pause to Assist, Let this case act | chip derivation | autonomy controller |
| Trust strip, agreement, graduation, closed-case review | none | agreement computation, review sampler |
| What it changed, revert, Planned next, Stop before this | Later frames on Summary | Act contract |
| Tell, Do, evidence row actions, add or rule out an explanation, remember and withdraw | Ask composer | typed actions and re-plan; new ADR superseding 0015 |
| Explanations for investigation cases | placeholder tab | cases on the hypothesis loop |
| Unified signed record | merged Record list | case record and write path |
| Trust scores, floor, drop line, rescue that learns, noise that learns | B3, B6 labels | source calibration and triage scoring |
| Blind-spot register, standing rules | Memory tab, gaps | Coverage |
| Pulse, Fleet Wall, Coverage pages | B2, B3 aggregates | phase-2 pages |
| Tool permissions | none | per-tool class policy |
| Custom and role-gated commands, blast count, fleet-wide directions | SH-P5 fixed set | commands registry, typed-action preview |
| Multi-user: Team, invites, View as, watchers, Assign to me | none | roles and personas |
| Hold with reason and duration, bulk actions on Cases | none | hold model |
| Live push | polling | event fan-out |
| Retention, feedback destination | none | Settings phase 2 |

## 16. Decisions

Decided here so nothing is left for the groomer or a human to settle. Each one is grounded in the codebase or the library's own documentation and can be reversed by a later revision of this document, not by a ticket.

| # | Decision | Grounds |
|---|---|---|
| D1 | **Stay on Tailwind 3.4 and run shadcn/ui in its Tailwind v3 mode.** `components.json` sets `tailwind.config` to `tailwind.config.cjs`, `cssVariables: true`, `baseColor: neutral`, style `new-york`. shadcn's semantic variables (`--background`, `--foreground`, `--primary`, `--border`, `--ring` and the rest) are defined once from the design tokens in `tokens/tokens.css`, so shadcn components and Vigil components read the same values. Preflight stays off; the console's own reset in `styles.css` stays until cut-over, and a one-line base rule sets the default border colour that shadcn components expect. The Tailwind 4 upgrade is phase 2. | shadcn's documentation still carries the v3 path: `components.json` takes a `tailwind.config` path for v3 projects and leaves it empty for v4, and its framework guides note v4 is the recommendation for new projects, not a requirement. Upgrading now would touch about 2,400 `className` lines in screens phase 1 is replacing anyway, and would fight the preflight-off reset. |
| D2 | **Dark is the default theme.** Light is complete and equal, switchable from the profile menu, and remembered per browser. | Today's default base is the dark `slate` swatch (`shell/bg.ts`, first swatch is the default), so existing installs keep what they have. The design package ships both themes. |
| D3 | **"Share feedback" opens a GitHub new-issue link** on the Vigil repository with the `console-feedback` label and a prefilled title and body carrying the page and the console version. The URL base is a build-time setting so a private deployment can point it at its own tracker or a mailto. No Settings › Feedback screen in phase 1. | Vigil is Apache-2.0 and developed on GitHub; there is no feedback endpoint in the API today and the destination question is what rev 1.1 left open. A link costs one PR and journals nothing, which is correct for phase 1. |
| D4 | **The case composer's Ask conversation is private to the user in phase 1** and is stored as a normal conversation with a `case_id` and a `page_context` on it (B10). The note under the composer reads "Private to you · Ask only". Phase 2 promotes case conversations to the case record. | The `conversations` model carries `user_id`, `agent_id` and `model` and no case reference; adding two nullable columns is B-class. Making it team-visible would mean listing other users' conversations, which is a permissions change phase 1 does not make. |
| D5 | **"What it changed" and "Planned next" on the Running summary render as headings with one explanatory line and an ⓘ, disabled (class Later).** They take no more than one row of height each and appear only in the Running mood. Nothing else on the case page renders a Later frame; every other deferred block is Omitted. | The team asked for a trailer; these two frames are the clearest picture of Act. Full empty frames would push real content below the fold on a 900 px board. |
| D6 | **Vendor logos come from the design package's `assets/logos/` and `assets/sources/`, shipped into `clients/web/src/assets/vendors/`, and the integration catalog gets a `logo` field per entry.** Where the package has no logo for a vendor, the row shows a neutral category icon with the vendor's name until a logo lands. CrowdStrike is in that state on day one and is not a phase gate; adding a logo is a one-file PR. | `config/integrations.ts` has no icon field today and `src/assets` holds only the Vigil logo, so the field and the asset folder are needed for any vendor logo, not just this one. |
| D7 | **Settings sections with no board** (Services, System, General, Developer) become one "System" tab at the end of Settings under the new tokens and shell. Appearance is removed; the theme switch lives in the profile menu; the accent and base swatches are dropped in favour of the design's two themes. | Keeps four working screens without redesign work; the design's token model has no per-install accent. |
| D8 | **Current screens with no phase-1 page** (Analytics, Case metrics, AutoOps, AI decisions, Health) stay reachable under the "More" menu with tokens only. Phase 2 retires them into Pulse, Fleet Wall and Coverage. | Removing them would drop working function; redesigning them is phase-2 work by another name. |
| D9 | **Roles stay as they are** (Viewer, Analyst, Senior Analyst, Manager, Admin) and no phase-1 screen depends on role beyond today's permission gate. Personas and the three-role model arrive with multi-user in phase 2. | "No multi-user" in the team's scope; today's roles table and permission JSON are untouched. |
| D10 | **Minimum width 1440 px, fluid above, as the boards specify.** Below 1440 px the console scrolls horizontally rather than reflowing. | The boards are drawn at 1440 and the design package states the root minimum; supporting narrower widths would be design work the package does not contain. |

## 17. Suggested epics

One epic each; the groomer decides PR size. Foundation first; pages after the shell; Settings and Agents & workflows are independent of the rest.

1. Foundation: tokens mapped onto shadcn's variables (D1), fonts, icons, vendor logo assets and the catalog `logo` field (D6), shadcn primitives, Vigil primitives (card, row stack, tab strip, state pill, info button, hold-to-confirm, undo fuse, status mark, step player, duration picker, scrub field).
2. Shell: header, nav with More menu, autonomy chip, status line (B4), profile menu, theme, drawer host, toasts.
3. Command bar and built-in commands (SH-P4, SH-P5).
4. Needs-a-person read endpoint (B1) and the Home page (section 7, B8).
5. Onboarding wizard, first run, tour, demo data (section 6, B7, B9).
6. Overview (section 8, B2, B5, B6).
7. Triage queue (section 9, B3).
8. Cases list (section 10, list).
9. Case page: header, tabs, Summary moods, Details panel (section 10, B11).
10. Case page: Explanations, Evidence, Checked, Memory and blind spots, Record tabs.
11. Case composer in Ask mode and citation chips; Ask Vigil dock and history (sections 10 and 11, B10).
12. Agents & workflows (section 12, B12).
13. Settings: AI models, Integrations, Custom integration (section 13).
14. Settings: Alert collection, SLA, Limits and autonomy, Data and uploads, System tab (section 13, B9).
15. Cut-over: retire the old rail, styles.css, Appearance, and the old tests; move legacy screens under More.
