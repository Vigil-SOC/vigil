# Vigil Console · design system

The console's design system: vocabulary, the visual system, layout rules and the component index. Exact values are in `tokens/`. There are no screen mockups; the running console is the reference for how a screen is laid out.

## 2. Vocabulary

| Use | Not | Notes |
|---|---|---|
| Needs you | Waiting on you, Needs attention, Pending approval | Chip, tab, island, tile label. Long form "Needs your attention" only as a page heading (Home). |
| Assist · asks before changes | Ask before changes, Approve mode | Tier 1. Agents read on their own, ask before any change. |
| Act · reversible changes on its own | Act on reversible changes, Autonomous | Tier 2. Reversible changes run and report after; anything that cannot be undone still asks. |
| — | Suggest only, Advise, Propose | Removed. Two tiers only. |
| Alert | Trigger, detection, signal | Product term kept. Alerts are triaged into cases. |
| Case | Incident, investigation, ticket | The unit of work. "Combined from N alerts". |
| Explanation | Hypothesis | What the agents are testing. Statuses: forming, standing, weakened, ruled out, proven. |
| Evidence row | Finding, IOC | One observation with source, tier, stance (supports / goes against / neither) and what it bears on. |
| Checked | Queries, tool calls | Every question asked of a tool, with latency. |
| Blind spot | Gap, missing telemetry | A question no source could answer. |
| Trust floor | Confidence threshold | Sources below it are shown but not counted. |
| Handled without a person | Auto-closed, fully automated | Closed-case review samples these. |
| Hold | Snooze, defer | Reason from a closed set + duration. |
| Revert | Rollback, undo (for agent changes) | Undo is for the user's own actions; Revert is for an agent's change. |
| Good / Fair / Poor | OK / Warning / Critical, green / amber / red | Usage vs limit: Good < 75%, Fair 75–90%, Poor > 90%. Rates: Good ≥ 95%, Fair 85–95%, Poor < 85%. |
| Ask Vigil | Chat, Copilot, Assistant | The composer. On a case: Ask / Tell / Do. Off a case: private to the user. |
| Agents & workflows | Automations, Playbooks | Nav label. |

Time is UTC, 24-hour, "14:22". Durations "4 min 12 s", "7 h left". Money "$0.42". Percentages "96%".

## 6. Visual system

Tokens in `tokens/`. Two themes, neutral grays, one accent. Summary:

- **Surfaces**: page `bg0` → panel `bg1` → card interior `bg2` → hover `bg3` → track `bg4`. Cards: `bg1`, 1px `ln0`, radius 14, padding 16/18. Row stacks: 1px `ln0` gaps with `bg2` rows, radius 10.
- **Type**: Plus Jakarta Sans for UI, Roboto Mono for IDs/numbers/code. Section titles 13px/650. Body 13px. Meta 12px `tx2`. Nothing under 11px.
- **Controls**: buttons 28px (secondary, radius 10) and 36px (primary/action bar, radius 10). Tabs 38px with 2px accent underline. State pill 22px, radius 999, weight 700 with a 7px dot. Count chips 11px mono.
- **Levels**: Good `good`, Fair `fair`, Poor `poor`; Needs you and destructive use `poor`; human contribution uses `vio`.
- **Motion**: rise (8px, 200ms) for new blocks, slide-in for drawers, fuse (8s linear) for undo, blink for pending, breathe for the live step dot. Respect reduced motion.

## 7. Layout

- Root is fluid with no minimum width; tables wider than their space scroll inside their own card. Header 52px (logo · command bar ⌘K · autonomy chip · avatar; the Needs you island is phase 2) + nav 46px.
- Case drawer 75% width over a scrim; expand toggles to 100% (icons `expand`/`shrink`). Side panel 280px. Drawer z 28: above page, below header (30) and menus (60).
- Chat dock 400px when off a case.

### Width tiers

Content layout follows the width the page actually gets, not the window: the side bar and the Ask Vigil dock both take width. `.main` is a named container (`main`), and content rules are `@container main (…)` queries; only the header, the nav and the dock itself stay on `@media`. The tiers are written once in `styles.css` (on `.main`) and repeated as literals, since a query can't use `var()`.

| Tier | Content width | Tables |
|---|---|---|
| full | ≥ 1400px | Every column at full width |
| compact | 1100–1399px | Columns at compact widths |
| narrow | 700–1099px | Compact widths; a table that still doesn't fit scrolls inside its card |
| reflow | < 700px | One column |

- **Columns never hide with nothing docked.** Tables narrow, then scroll inside their card.
- **Low-priority columns step aside only while Ask Vigil is docked** and the content is under 1400px. Their `th` and `td` carry `col-low` (`ColumnDef.className` in `DataTable`); a `col-low-note` in the table header says they are hidden. They come back when Ask Vigil closes. Drawers overlay the page, so they hide nothing.
- **The main column and the column with the row's link never hide.** The main column never narrows below `--col-main-min` (200px): a table's minimum width is its fixed tracks plus that, lowered by the hidden columns while they are hidden.
- **Reading pages** (Settings, the case Summary, workflow detail, setup) cap at `--page-max` (1280px). **Data pages** keep the full width; their free text caps at `--text-max` (75ch).
- **Fixed overlays portal out.** As a container, `.main` would trap `position: fixed` descendants, so dialogs and side drawers render through `OverlayPortal`, outside `.main`.

### Screen rules

Check every screen you change against these at 1440px and at 1920px, in a real browser with real data.

- **Nothing sideways.** No content is reachable only by scrolling sideways, and a row's primary action is on screen without scrolling. An element whose `scrollWidth` exceeds its `clientWidth` breaks this rule, except a table that scrolls inside its own card.
- **Prose wraps, then stops.** In tables, descriptions, summaries and other free text wrap, so a long one cannot decide a column's width, and stop at two lines with an ellipsis, so it cannot make its row tall. The full text must be reachable from the keyboard, and a `title` on hover is never the only way: the row opens on Enter and Space as well as a click, and shows it there. In a row or card that opens nowhere, the clamp lifts while focus is inside it (`:focus-within`) if it has a control to tab to, and the text wraps in full if it has none. With WCAG text spacing applied (1.4.12), nothing is cut off without one of these. Only IDs, numbers, times, chips and action buttons stay on one line.
- **Rows stay even.** A cell that would grow its row far past its neighbours (a stack of chips, for example) shows the first few and "+N".
- **Names match the nav.** A screen's heading uses the nav label (§2): the Agents & workflows screen is headed "Agents & workflows".
- **Tab strips are uniform.** Show a count on every tab of a strip or on none.
- **One accent.** Active underlines, in the nav and in tab strips, use the accent token. No second underline colour.
- **Floating controls cover nothing.** The Ask Vigil button and other floating controls leave a row's content and actions visible. Pad the scroll area under them.

## 8. Component index

See `components/`. Phase 1 ships: card, row-stack, tab-strip, state-pill, info-button, hold-to-confirm, undo-fuse, needs-you-strip, decision-block, citation-chip, composer (Ask mode only). The sign-in, session and MFA screens use confirm-you. Not in phase 1 and not in this folder: tier-control, trust-strip, revert-window, parsed-action, evidence-row-actions.
