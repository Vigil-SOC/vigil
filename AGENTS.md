# Vigil · notes for coding agents

- **Domain terms**: `CONTEXT.md`. **Product intent**: `INTENT.md`.
- **Console UI** (`clients/web`): `docs/design/console/DESIGN.md` sets the vocabulary, visual system and screen rules; `PRD-phase1.md` beside it is canonical for behaviour. Before you open a PR that changes what a screen renders, check every screen it touches against DESIGN.md §7 "Screen rules" in a real browser, at 1440px and 1920px. The frontend's unit tests run in jsdom, which does not lay a page out.
- **Reference designs**: `docs/design/console/boards/png/` holds a render of every phase-1 board, and `docs/design/console/README.md` maps each screen to its board. Put your screenshot beside the board for that screen and match its density and conventions. Boards decide appearance, the PRD decides scope: fix what the issue's change touches, and leave a redesign toward the board to an issue of its own.
- **Running the console**: the backend serves on :6987 and `npm run dev` in `clients/web` serves the console on :6988, proxying API calls to the backend.
