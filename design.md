# Bunkmate — Design System v1.0 (Neo-Brutalism)
**16 Aug 2026 · Companion docs:** `prd.md`, `implementation-plan.md`
**Source of truth:** `tokens.css` + `components.css` (delivered in session; drop into Flask `static/css/`). Living style guide persisted as the desktop artifact **bunkmate-design-system**.

## Direction (user decisions, 16 Aug)
Cream base + vibrant accents (per the Neo-Brutalism component-library and cheat-sheet references). Light theme only in v1 — everything runs through CSS custom properties, so dark mode later is one `[data-theme="dark"]` override block. Hexes taken directly from the user's reference images.

## Style rules
Black `#111111` strokes (2px default, 3px cards) and hard shadows — offset only, blur 0, opacity 100% (`2/4/6/10px` steps). No gradients, no soft shadows, no glassmorphism. Motion is physical translate only: hover lifts (−2,−2) + shadow grows; press slams flat (+4,+4, shadow 0); ≤150ms; `prefers-reduced-motion` respected. Radii: 8 buttons/inputs, 12 cards, 16 modals, pill for chips; `--radius-0` reserved for deliberate hero moments.

## Color tokens
Surfaces: `--paper #FAF3EB` (app bg), `--card #FFFDF8`, `--ink #111111`, `--ink-muted #4A4640`.
Vibrant accents: yellow `#F4D738` (primary actions), purple `#A388EE` (brand accent), cyan `#69D2E7` (upload/info), pink `#FFB2EF`, red `#FF6B6B`, orange `#FF7A5C`, blue `#87CEEB`, green `#90EE90`.
Muted pastels (tinted cards/stripes): `#FDFD96`, `#E3DFF2`, `#DAF5F0`, `#BAFCA2`, `#FCDFFF`, `#F8D6B3`.

**Semantic status (reserved — the app's one color language):**

| State | Fill (ink text) | Deep (text on cream) | Meaning |
|---|---|---|---|
| safe | `#90EE90` | `#1A7F37` | above limit / skippable |
| warn | `#F4D738` | `#8A5A00` | tight budget |
| danger | `#FF6B6B` | `#C81E2E` | below limit / must attend |
| pending | `#A388EE` (soft `#E3DFF2`) | `#6C2BD9` | NU lectures — unknowns, deliberately outside the verdict trio |

All ink-on-fill pairs ≥6.5:1, deep-on-cream ≥4.5:1 — WCAG AA verified programmatically. Status never by color alone: always dot + label (badges), tag text (verdict chips).

## Typography
Display **Archivo Black** (verdicts, hero numbers, titles) · Label **Lexend Mega 700 uppercase** (buttons, eyebrows, table headers) · Body **Public Sans** 400/700/800 · Mono **Space Mono** for *every* number, %, date, time, range. Scale (rem): 12/14/16/18/22/28/36/48/64. Google Fonts import is in the tokens file header.

## Spacing / structure
4px base scale: 4, 8, 12, 16, 24, 32, 48, 64 (`--sp-1..8`). Focus = 3px dashed ink outline, offset 3px.

## Components implemented (components.css)
- **Buttons** `.btn` — primary (yellow) / secondary / accent (purple) / danger / ghost; sm/md/lg/pill; hover-lift, press-flat, disabled, focus-visible.
- **Cards** `.card` — flat/lift, pastel tints (`--yellow/--purple/--cyan/--pink`), `--ink` inverse, clickable.
- **Badges** `.badge` — mono pill, dot + label; safe/warn/danger/pending/neutral.
- **Verdict chips** `.verdict` + `.day-strip` — GO/SKIP hero strip: `--skip` green "SKIP OK", `--part` amber "PARTIAL", `--go` red "MUST GO", `--off` dashed no-class, `--today` marker.
- **Stat tiles** `.stat` — Archivo Black number, label eyebrow, mono meta; status tints; `--hero` size.
- **Attendance meter** `.meter` — pill track, status fill, dashed limit tick with % label.
- **Subject table** `.subjects` — ink header bar (Lexend Mega), mono right-aligned numbers, paper striping, soft-yellow hover → per-lecture drill-down.
- **Banners** `.banner` — warn (gap: "no report covers 13.08 → 21.08" with mono `.range` chip + copy button), info (stale-NU), danger, success.
- **Diff list** `.diff-row` — NU → P/A transitions after upload.
- **Forms** — `.input`/`.select` (lift on focus), error state (danger border + message), hint, checkbox (✕ mark), toggle, tabs, `.dropzone` (dashed cyan PDF drop).
- **Nav** `.nav` + logo chip · **toast** (ink w/ purple shadow) · **modal** (alias-confirmation pattern).
- Utilities: `.text-safe/warn/danger/pending`, `.mono`, `.eyebrow`, `.sr-only`.

## Do / Don't (enforced in reviews)
Do: one dominant accent per screen; mono for trustworthy numbers; label every status. Don't: gradients/blur; status fills as decoration; >3 accents per component; opacity/scale animation; verdict by color alone.

## Deferred
Dark mode (token override block), print styles, PWA icon set, empty-state illustrations, chart palette (run through the dataviz validator when charts appear in Phase 2+).