# Vigil Console design system

The console's look, in one place: what colour, type, spacing and components a screen uses. There are no screen mockups to match. The running console is the reference for how a screen is laid out, and a change to a screen is judged on whether it works well and fits the screens around it.

- **`DESIGN.md`** holds the vocabulary, the visual system (surfaces, type, controls, Good/Fair/Poor levels, motion), layout and its *Screen rules*, and the component index.
- **`tokens/`** holds the colour, type, radius, control and motion values for both themes. `tokens.css` is imported by the console; `tokens.json` carries the same values. Where `clients/web/src/styles.css` differs, the tokens win.
- **`components/`** holds one spec per shared component.
- **`MEDIC.md`** holds the Medic console's requirements (UX-1…UX-30: the Health card, the status-line fact and the `/medic` screen) and the tokens and components each surface uses.

## Assets

`assets/logos/` holds the Vigil lockups (dark and light), app icon and mark, the DeepTempo shield (colour and mono), and `vendors/` (vendor marks for the integration catalog; each file's source is in its README). They ship into `clients/web/src/assets/vendors/` and the integration catalog points at them with a `logo` field; a vendor without a logo here shows a category icon with its name. `assets/icons/` holds the 82-icon line set (24 viewBox, 1.8 stroke, `currentColor`) with `icons.json`.

Fonts: Plus Jakarta Sans 400–700 and Roboto Mono 400–500, self-hosted by the console.
