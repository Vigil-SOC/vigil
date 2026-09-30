# Vigil Console design reference, phase 1

This folder is what an implementer or groomer needs for console work in phase 1, and nothing else. It is a trimmed copy of the Vigil Console v7 design package (claude.ai/design export of 2026-09-30), limited to the boards, components and assets phase 1 uses.

## What is canonical

1. **`PRD-phase1.md`** decides scope and behaviour. When anything in this folder disagrees with it on what to build, the PRD wins. It has no open decisions; section 16 records every decision and its grounds.
2. **Boards** decide appearance. `boards/png/` holds a render of every phase-1 board in dark and light at the board's own size (1440 × 900 for app boards, so content below the fold is only in the HTML), plus the three case-page moods. `boards/*.dc.html` are the interactive originals (open one in a browser with `support.js` beside it); grep them for exact inline styles, spacing and copy. The live canvas is at https://claude.ai/artifact/LfdCAGfXzJaWzDASis8pWP and wins over the PNGs if they ever differ.
3. **`DESIGN.md`** (phase-1 edition) holds vocabulary, the visual system, layout and the component index. **`tokens/`** holds the colour, type, radius, control and motion values for both themes. **`components/`** holds one spec per component phase 1 ships.

## Read the boards with the PRD open

Every app board shares one shell, and that shell was drawn for the full product. The following appear on every board and are **phase 2**; the PRD names the class for each (Omitted, Later, Not measured yet):

- the Needs you island in the header ("6 · Needs you · 214 running");
- the Pulse, Fleet Wall and Coverage tabs (MainP2 is the only board drawn with the phase-1 nav);
- "View as" in the profile menu;
- slash-command chips beyond the built-in set in PRD SH-P5;
- on the case page: the tier control, trust strip, "Approve narrower", Hold with duration, Tell and Do modes, revert rows;
- in the Settings sidebar: Team & access and Feedback & usage data (phase 2), Notifications (no board exists; not in the PRD), Appearance (removed per PRD D7).

Numbers on the boards are sample data (1,284 alerts, 73 cases, 214 agents, 34 decisions) and are never defaults. PRD PF7 in rev 1.1 still applies: no policy number is hard-coded in the UI.

## Boards in this folder, by PRD section

| PRD section | Boards |
|---|---|
| 5 Shell | every app board for the header and nav; `Search` for the command bar; `MainP2` for the phase-1 nav |
| 6 Onboarding | `OnboardingFlow`, `Onboarding`, `OnboardingConnect`, `OnboardingModel`, `OnboardingAgents`, `OnboardingLimits`, `OnboardingDone`, `FirstRun`, `Tour`, `TourAsk` (the tour's second stop is the Home Needs you section, not the island; `TourIsland` is not included) |
| 7 Home | `MainP2` only (`Main` and `HomeBusy` are the phase-2 Home and are not included) |
| 8 Overview | `Overview`, `OverviewAlert`, `OverviewEmpty`, `OverviewFull` |
| 9 Triage queue | `Triage` |
| 10 Cases | `Cases`, `CaseDetail` (moods needs, act, closed in `png/`), `CaseInvestigation`, `CaseComments` |
| 11 Ask Vigil | `PulseChat` for the dock layout only (Pulse itself is phase 2), `ChatHistory`, `TourAsk` |
| 12 Agents & workflows | `Agents`, `AgentEditor`, `SkillEditor`, `WorkflowRun` (`ToolPermissions` is phase 2 and not included) |
| 13 Settings | `Settings`, `SettingsIntegrations`, `SettingsCustom`, `SettingsCollection`, `SettingsSLA`, `SettingsLimits`, `SettingsData` (`SettingsTeam`, `SettingsFeedback` are phase 2 and not included) |
| reference | `DesignSystem` (tokens, type, Good/Fair/Poor, components, shadcn/ui mapping in section 08), `Logo` |

Not included on purpose: `Pulse*`, `FleetWall`, `Coverage`, `ToolPermissions`, `Main`, `HomeBusy`, `TourIsland`, `SettingsTeam`, `SettingsFeedback`, the `*V1` case boards (deprecated), `Handoff`, `HandoffData`, `Index`, and the five component specs for phase-2 components.

## Assets

`assets/logos/` holds the Vigil lockups (dark and light), app icon and mark, the DeepTempo shield (colour and mono), and `vendors/` (CloudTrail, Okta, Splunk). Per PRD D6 these ship into `clients/web/src/assets/vendors/` and the integration catalog gets a `logo` field; a vendor without a logo here shows a category icon with its name. `assets/icons/` holds the 82-icon line set (24 viewBox, 1.8 stroke, `currentColor`) with `icons.json`.

Fonts: Plus Jakarta Sans 400–700 and Roboto Mono 400–500. The boards load them from Google Fonts; production self-hosts them (PRD epic 1).

## Provenance

Full package and generator: exported 2026-09-30 from the canvas above; the untrimmed zip is kept outside the repo. Full-product requirements: PRD rev 1.1 (2026-09-29), superseded for phase 1 by `PRD-phase1.md`. Sizing and the phase split: `vigil-console-v7-assessment.md` (outside the repo).
