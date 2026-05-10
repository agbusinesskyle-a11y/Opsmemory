# Handoff: OpsMemory — Triage Tab

## Overview

This handoff covers the redesigned **Triage tab** for OpsMemory, the PWA that processes AI-extracted task candidates from Slack messages, meeting recaps, and file drops. Triage replaces the current "Review" tab as the operator's daily entry point: a dense, keyboard-driven inbox where each AI-proposed action (CREATE / UPDATE / COMPLETE / AMBIGUOUS) can be approved, rejected, edited, or snoozed individually or in bulk.

The design is the operator-facing answer to the four highest-friction items in the research artifact:

- **F1 — per-item click → expand → approve** (slow daily ritual): replaced by a focus pane that always shows the focused row's source + facts + validation, with single-keystroke actions.
- **F2 — no batch operations**: Linear-style multi-select (`X`, shift-extend, `⌘A`) with a sticky bulk bar that surfaces approve/reject/snooze for the selection.
- **F3 — no keyboard shortcuts**: 16 shortcuts mapped to Linear / Things / Todoist conventions.
- **F7 — no staleness affordance**: forecast strip + per-row age dots distinguish a 5-minute candidate from a 4-day-old one at a glance.

This is the artifact for **operator sign-off before any code change in `web/app.js`**. It is *not* production code to copy.

## About the Design Files

The bundled `Triage.html` + `triage.jsx` + `tweaks-panel.jsx` are a **design reference** — an interactive HTML prototype that demonstrates intended look, density, keyboard model, and state transitions against fake in-memory data. They are intentionally:

- **Single-file React + Babel-in-the-browser** for fast iteration; not how this should ship.
- **Wired to a hardcoded `SAMPLE` array**, not the real `review_items` API.
- **Detached from auth, push subscriptions, the outbox, and the service worker**.

The implementation task is to **recreate this design inside the existing OpsMemory PWA** (`web/index.html` + `web/app.js`, vanilla JS, no framework, render-functions returning HTML strings, top-level `state` object). Stay vanilla. Match the rendering patterns already used by `renderTask`, `renderReviewItem`, `renderSopItem`. Do **not** introduce React, a build step, or a bundler — that is a separate decision tracked in §14 of the research artifact and is not gated by this work.

If you decide a small framework (Preact / Lit / Alpine) is worth pulling in, raise it as a separate decision before starting; the default is "vanilla stays."

## Fidelity

**High-fidelity (hifi)** for visuals, typography, density, color, interactions, and keyboard model. Final hex values, exact pixel sizes, and concrete keyboard handlers are in this README. Recreate pixel-faithfully where the existing PWA's CSS allows; reuse existing tokens in `web/styles.css` where they already match.

**Phasing reminder.** §10 of the research artifact splits this into UI-1 → UI-7. This handoff is the union of UI-1 + UI-2 + a sliver of UI-3 (the forecast strip) + UI-7 visual polish. **Ship UI-1 first** (keyboard + selection, no schema change), then UI-2 (Triage rename + sub-views + sticky bulk bar — the snooze action requires a `review_items.snoozed_until` schema bump documented below).

---

## Screens / Views

There is one top-level view in this handoff: the **Triage tab** (`/triage` or whatever the existing tab routing convention is). It has four sub-views and a detail pane.

### Layout

A 2-column app shell:

- **Left rail**: 204px fixed-width sidebar with workspace nav + spaces list + user footer.
- **Main area**: `grid-template-rows: auto auto auto 1fr` —
  1. Topbar (breadcrumb + global keyboard hints + detail-pane toggle), ~38px.
  2. Header (title "Triage" + sub-view tabs + filter affordance), ~78px including sub-tabs.
  3. Forecast strip (5 cards, equal-flex), ~80px.
  4. List + Detail (grid `minmax(0,1fr) 380px` when detail open, full-width when closed). Both panes scroll independently.

When the detail pane is closed, the list expands to fill and uses a roomier column grid (see Components → List Row).

### Sub-views (tabs above the list)

Order, label, content rule:

1. **Inbox** — `status === "pending"`. Default landing tab. Count = #pending.
2. **Stale** — `status === "pending" AND age >= 3 days`. Count = #pending stale.
3. **Snoozed** — `status === "snoozed"`. Count = #snoozed (regardless of when they unsnooze).
4. **Completed today** — `status === "approved" OR rejected today`, plus a server-fed `completed_today` stream of items handled before the page loaded. Count = sum.

Sub-view switch resets the forecast filter.

### Detail Pane

Always visible by default at ≥1024px viewports; toggleable via the topbar icon. Contents in vertical order:

1. **Header**: action chip · `conf 0.91` · `pending 3d` · status chips (approved / rejected / snoozed) · the candidate summary as `<h2>`.
2. **Source card**: source-kind icon, author, channel/file, timestamp, `open raw event →` link, then the **quoted source text** (the Slack message / recap excerpt / file caption that triggered the candidate).
3. **Proposed facts** (definition list): summary, business, assignee, priority, due_at. Missing values render as `— missing —` / `— unassigned —` / `— none —` in dim foreground.
4. **Retrieved candidates**: list of `{name, conf}` matches against existing tasks. Empty state: "No prior tasks matched — will create new."
5. **Validation**: zero-state shows green check + "No issues."; non-zero shows amber warn icon per row.
6. **Action row** (sticky bottom of detail): four buttons — Approve (primary, accent-filled) / Edit + approve (`3`) / Reject (`2`, danger-tinted) / Snooze (`H`). Each shows its keyboard shortcut as a `pill-kbd` inside the button.

When the focused row is in the **edit-then-approve** state (entered via `3`), the body is replaced by a form with: summary text input, business select (RedHot / Borderline / membership-scoped), assignee select, priority select, due text. Save & approve commits with `status="approved"`; Cancel returns to read mode.

---

## Components

All measurements from the prototype's CSS. Use them as truth.

### Left rail

- Width 204px, background `--bg-rail`, right border `--bd-soft`.
- Brand row: 14×14 gradient square (accent → +40° hue) + "OpsMemory" at 13px / 600.
- Section labels: 10.5px / 500 / uppercase / `letter-spacing: .08em`, color `--fg-dim`.
- Nav items: 26px tall, 12.5px text, 5×10 padding, 6px radius, icon 14×14. Active = `--bg-elev2` background, `--fg` text.
- Spaces list shows a 8×8 colored square per space (RedHot = `--c-redhot`, Borderline = `--c-border`).
- Footer: 20×20 avatar, name, `?` keyboard pill that opens shortcuts.
- **Membership scoping rule (§13.1 of artifact):** the Spaces list, the Triage default scope, and saved views must filter by the current user's `business_memberships` rows. Hardcoded `redhot` / `borderline` strings in this prototype must become data.

### Topbar

- Padding `10 16 8`, bottom border `--bd-soft`.
- Breadcrumb: `Workspace / **Triage**` at 11.5px.
- Right side: `⌘K` pill, `?` pill, detail-toggle icon button. Each `iconbtn` is 26×26, 6px radius, `--bd-soft` border.

### Header (title + sub-tabs)

- Title: 18px / 600 / `letter-spacing: -0.01em`. Subtitle: 12px / `--fg-mut` showing `{count} pending · avg confidence {pct}%`.
- Sub-tab strip: bottom border `--bd-soft`, tabs `padding: 8 11`, 12.5px text. Inactive `--fg-mut`; active `--fg` + 2px bottom border in `--fg`. Counts in 11.5px tabular numerics, dim.
- Right side of the strip: `Filter` button collapses into a 200px-min search input on click (`/` or `⌘K` opens it).

### Forecast strip

- 5 equal-flex cards in a row. Each: `--bg-elev` background, `--bd-soft` border, 8px radius, `padding: 10 12`.
- Hover and active state: lift to `--bg-elev2` + `--bd` border.
- Click toggles a forecast filter overlaid on the current sub-view (filters compose).
- Card contents: 11px uppercase label · 20px / 600 number (tonal: amber for "Stale 3d+", red for "Stale 7d+" and "Due today / urgent" when count > 0) · 18px sparkline at the bottom.
- The five cards (Triage flavor): **Fresh today**, **Pending 1–3d**, **Stale 3d+**, **Stale 7d+**, **Due today / urgent**.

### List header

- 30px tall, sticky to top of list, `--bg` background, `--bd-soft` bottom border.
- Columns (detail open): `50px 78px 70px 130px minmax(0,1fr) auto`.
- Columns (detail closed): `60px 100px 86px 170px minmax(0,1fr) 240px`.
- Labels: 10.5px / 500 / uppercase / `letter-spacing: .06em`, color `--fg-dim`. "Flags" right-aligned.

### List row

- Height: `--row-h` token (32px compact / 36px cozy / 40px comfy). Default = compact.
- Bottom border `--bd-soft`. Cursor `default` (not pointer — Linear convention).
- States, in priority order:
  - **Focused** (`.focused`): `--bg-row-foc` background + 2px accent left bar (`::before`).
  - **Selected** (`.selected`): `--bg-row-sel` background. Selected+focused = the same accent left bar over the selected bg.
  - **Approved** (`.approved`): opacity 0.42, summary gets strikethrough.
  - **Snoozed** (`.suppressed`): opacity 0.55 — only seen in Snoozed sub-view.
- **Age cell**: 14×14 checkbox, optional 6px stale dot (`--c-amb` for 3–7d, `--c-redhot` for 7d+), age string. Checkbox: empty when unselected, accent-filled with white check when selected. Hovering or focusing the row darkens the checkbox border to `--fg-dim`.
- **Action cell**: chip with 8×8 leading swatch (`border-radius: 2px` for CREATE/UPDATE/COMPLETE; circle for AMBIGUOUS). 10.5px / 600 uppercase. Colors:
  - CREATE: `--c-create` (`oklch(0.74 0.13 150)`)
  - UPDATE: `--c-update` (`oklch(0.72 0.13 235)`)
  - COMPLETE: `--c-complete` (`oklch(0.72 0.13 295)`)
  - AMBIGUOUS: `--c-amb` (`oklch(0.78 0.13 85)`) — abbreviated as "Ambig."
- **Conf cell**: tabular-nums 2-decimal score + a 34px max bar (54px when detail closed). Bar fill color tiers:
  - `>= 0.85` → `--c-create` (green)
  - `>= 0.70` → `--c-update` (blue)
  - `>= 0.55` → `--c-amb` (amber)
  - `< 0.55` → `--c-redhot` (red)
- **Source cell**: 11×11 source-kind icon (Slack / doc / file) + truncating channel/file name. 11.5px text.
- **Summary cell**: 12.5px text, single-line truncate. When approved, prefix with a green check icon.
- **Flags cell**: right-aligned chip flow, no wrap. Possible chips:
  - `RedHot` → `--c-redhot` background tint
  - `Borderline` → `--c-border` background tint
  - `due {date}` → neutral `--bg-elev2`; if `dueAt === "Today"`, tint with `--c-redhot` instead
  - `SOP?` → blue (`--c-update`) tint when any flag starts with `sop:`
  - `conflict` / `ambiguous` → amber tint
  - `urgent` → red tint
  - Status chips when not pending: `approved` (green tint), `snoozed · {when}` (neutral), and on rejected items the rejected reason.

### Detail pane

- Width 380px. Background `--bg-elev`. Left border `--bd-soft`. Independent scroll.
- Header: 14×18 padding, bottom border `--bd-soft`. Meta row in 11.5px `--fg-mut`. Title in 15px / 600 / -0.005em letter-spacing.
- Body: `display: flex; flex-direction: column; gap: 18px; padding: 14 18`.
- Section headers (`.sec-h`): 10.5px / 500 / uppercase / `letter-spacing: .06em`, color `--fg-dim`, 6px bottom margin.
- KV grid (`.kvgrid`): `grid-template-columns: 96px 1fr`, 14px col-gap, 5px row-gap, 12.5px text. Keys in `--fg-mut`. Tabular nums on the values.
- Source card: `--bg` background, `--bd-soft` border, 8px radius, padding `10 12`. Quote in 12.5px / 1.5 line-height with `text-wrap: pretty` and surrounding double-quotes.
- Candidate rows: same `--bg` card pattern, justify-between, 12.5px label and 11px confidence on the right.
- Action row: top border `--bd-soft`, 12 18 padding, 6px gap. Buttons are 28px tall, 6px radius, 12px text. Each carries an inline `pill-kbd` for its shortcut. Approve = primary (`--acc` filled, white text). Reject = `.danger` (red foreground, hover red-tinted bg).

### Sticky bulk bar

- Centered horizontally, 18px from bottom. Slides up on `selected.size > 0` via transform.
- Width: hugs content. Background `--bg-elev2`. Border `--bd`. Radius 10px. Drop shadow.
- Inner row: count chip · per-action breakdown · Approve (primary) · Reject · Snooze · Clear.
- The per-action breakdown groups the selection by action type and emits e.g. `CREATE 3 · UPDATE 2 · AMBIGUOUS 1` so the operator knows what they're about to apply before they press Approve.
- **Apply behavior**: bulk approve hits the existing chunk-4 transactional apply path. The modal grouping shown in §8 of the artifact (`8 CREATE_TASK -> apply / 1 AMBIGUOUS -> skipped` etc.) is not in this prototype — add it as the confirmation step before commit when the selection contains AMBIGUOUS items or items with conflicts.

### Reject modal

- 460px wide, centered, 12px radius, `--bg-elev` background.
- Body: preset reason chips (Wrong / Duplicate / Out of scope / Already handled / Spam) + a textarea for the shared reason.
- Affecting summary: "CREATE 3 · UPDATE 2" so the operator sees the breakdown before committing.
- Footer: Cancel + danger-tinted "Reject N" button.

### Snooze modal

- Same shell.
- Body: 4 quick-date tiles (`+3h`, `Tom.`, `Sat`, `Mon`) in a 4-col grid + a custom-date text input below.
- Each tile shows the canonical label on top and the natural-language target underneath (e.g. "this Saturday"). Selecting custom auto-deselects the tiles.
- Persists to a new `review_items.snoozed_until` column — schema bump `0008_snooze.sql` per §10 phase UI-2.

### Shortcuts overlay

- Bottom-right floating card, 340px wide.
- Sections: Navigation / Triage actions / Global. Each row is `label … kbd-pill(s)`.
- Toggles via `?`. The `?` pill in the topbar and footer also toggles it.

### Toast

- Bottom-center, 80px from bottom, 8px radius, `--bg-elev2`. 1.8s auto-dismiss. Used for "Approved N", "Rejected N", "Snoozed N until {when}", "→ Triage" (after `G` `R`).

---

## Interactions & Behavior

### Keyboard map (full)

| Keystroke | Action |
|---|---|
| `J` / `↓` | Move focus down |
| `K` / `↑` | Move focus up |
| `Shift+J` / `Shift+↓` | Extend selection down |
| `Shift+K` / `Shift+↑` | Extend selection up |
| `X` | Toggle selection on focused row |
| `Shift+X` | (handled implicitly via shift-extend on J/K; preserved for parity with Linear) |
| `⌘A` / `Ctrl+A` | Select all visible rows |
| `Enter` | Open / focus detail pane |
| `Esc` | Close search → clear selection → clear forecast filter (in that priority order); also closes any modal |
| `1` | Approve selection or focused row |
| `2` | Open Reject modal for selection or focused row |
| `3` | Edit-then-approve (single focused row only; toast if a multi-selection) |
| `H` | Open Snooze modal for selection or focused row |
| `?` | Toggle shortcuts overlay |
| `⌘K` / `Ctrl+K` | Open command/search input |
| `/` | Open filter input |
| `G` then `R` | Go to Triage |
| `G` then `T` | Go to Tasks |
| `G` then `S` | Go to SOPs |

**Implementation notes:**

- Keystrokes never fire while focus is in an `<input>` / `<textarea>` / `[contenteditable]` (except `Esc`).
- A modal traps `Esc` to close. When the inline edit form is open, `Esc` cancels.
- The `G`-prefix has an 800ms timeout window after which the prefix clears. Avoid double-binding `r` / `t` / `s` as standalone keys.
- Approving a focused row scrolls focus to the next visible candidate (do not advance focus past the list end).

### Selection model

- `selected: Set<id>` and `anchorIdx: number`. `X` toggles single. Shift-extend selects the inclusive range `[anchorIdx, currentIdx]` in display order — including ones that were already in the set (so re-shift-extending a smaller range deselects nothing; this matches Linear).
- Switching sub-view does **not** clear the selection (a selected ID can persist across sub-views), but the bulk bar only shows the count of items currently visible in the merged set. Decide product behavior here with the operator; the prototype keeps the full set.
- `clearSel()` happens on Esc, and after every successful bulk action.

### State transitions

| From | Action | To | Side effects |
|---|---|---|---|
| `pending` | Approve | `approved` | Toast "Approved N". Row gets opacity 0.42 + strikethrough summary + `approved` chip. Detail pane action row hides. |
| `pending` | Reject | `rejected` | Toast "Rejected N — reason". Row stays visible in Inbox in this prototype but in production should leave the Inbox query and surface in a "rejected today" sub-view if you add one. |
| `pending` | Snooze until X | `snoozed` | Toast "Snoozed N until X". Row leaves Inbox sub-view; appears in Snoozed sub-view with `snoozed · {when}` chip and 0.55 opacity. |
| `pending` | Edit + approve | `approved` (with patched fields) | Same as Approve, but the patched fields (summary / business / assignee / priority / dueAt) are applied first. |
| `snoozed` | (date passes) | `pending` | Out of scope for this prototype — server-side cron flips `status` back to pending when `snoozed_until <= now()`. |

### Forecast filter

- The five cards each map to a predicate over `pending` items. Click toggles the filter; clicking the active card clears it.
- Composes with the current sub-view: e.g. `Stale 3d+` while on `Inbox` shows the same set as the `Stale` sub-view but keeps you in the Inbox tab visually.
- `Esc` clears the forecast filter (after any selection clear).

### Search / filter input

- Triggered by `/` or `⌘K`, dismissed by clicking the `×` or pressing `Esc`.
- Filters `summary` + `src.channel` + `src.quote` (case-insensitive substring). Composable with sub-view + forecast.

### Detail pane open behavior

- Always shows the focused row's content.
- Opens automatically on `Enter`. The topbar toggle force-collapses or force-shows it.
- At narrow viewports (<1024px proposed; not yet implemented in the prototype) it should become an overlay drawer instead of a sibling grid column.

### Animations / transitions

- Bulk bar: 180ms ease translateY 120% → 0%.
- Modal backdrop: instant. Modal content: no entrance animation in the prototype — add a subtle 120ms scale(0.98 → 1) + opacity if your codebase already does this.
- Toast: instant in, instant out at 1.8s. Production should fade.
- No skeleton loaders specced — `review_items` is small and local; if perceived latency exists, add a 80ms-debounced "loading…" line in the row area only.

---

## State Management

In the existing `web/app.js` `state` object, add a `triage` slice. Suggested shape:

```js
state.triage = {
  subview: "inbox",            // "inbox" | "stale" | "snoozed" | "completed"
  forecastFilter: null,        // null | "fresh" | "warm" | "stale" | "vstale" | "urgent"
  search: "",
  searchOpen: false,

  items: [],                   // review_items joined with the source event quote + retrieved candidates
  completedToday: [],          // server-fed: items completed before page load

  focusIdx: 0,
  selected: new Set(),
  anchorIdx: 0,

  detailOpen: true,
  editingId: null,             // set by `3`, cleared on cancel/save
  modal: null,                 // { kind: "reject"|"snooze", ids: [] } | null
  showHelp: false,
  toast: null,                 // { msg } — auto-clears
};
```

### Data fetching

- Initial: `GET /api/review_items?status=pending` (existing endpoint, extend response to include the joined `source_event` quote, `retrieved_candidates`, and `validation` arrays so the detail pane has everything it needs without a follow-up roundtrip).
- Sub-view counts: server should return `{counts: {inbox, stale, snoozed, completed_today}}` in the same payload so the tab badges update without 4 round-trips.
- `completed_today`: returned in the same payload, scoped to the user's spaces.
- Approve/Reject/Snooze: existing chunk-4 apply path. Snooze needs a new mutation that writes `review_items.snoozed_until` (schema 0008).
- Bulk approve: send the array of IDs in one request; server applies transactionally; partial-success response shape: `{applied: [...], skipped: [...], conflicts: [...]}`. Render the conflict resolution sheet on partial success (out of scope for this prototype but spec'd in §8).

### Multi-space scoping (§13)

- All queries server-side filter by `business_memberships` for the current user.
- Pill rendering reads `state.user.spaces`, not a hardcoded list.
- Saved views (Phase UI-3) store `business_ids` in their query JSON.

---

## Design Tokens

All values come from the prototype's CSS variables. Drop these into `web/styles.css` as the canonical token set.

### Color (dark — default)

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0b0c0e` | App background |
| `--bg-rail` | `#0e0f12` | Left rail background |
| `--bg-elev` | `#131418` | Cards, detail pane, modal body |
| `--bg-elev2` | `#1a1c21` | Active rail item, sticky bulk bar, button surface |
| `--bg-row-h` | `#16181c` | Row hover |
| `--bg-row-sel` | `#1b2230` | Row selected |
| `--bg-row-foc` | `#202229` | Row focused |
| `--bd` | `#22242a` | Strong borders (buttons, inputs) |
| `--bd-soft` | `#1a1c20` | Hairlines (row dividers, section borders) |
| `--fg` | `#e6e7ea` | Primary text |
| `--fg-mut` | `#8d9098` | Secondary text, meta |
| `--fg-dim` | `#5e6168` | Tertiary, uppercase labels |
| `--fg-faint` | `#3e4047` | Disabled, separators |

### Color (light — inverse)

| Token | Value |
|---|---|
| `--bg` | `#f7f7f8` |
| `--bg-rail` | `#efeff1` |
| `--bg-elev` | `#ffffff` |
| `--bg-elev2` | `#ffffff` |
| `--bg-row-h` | `#f0f1f3` |
| `--bg-row-sel` | `oklch(0.92 0.04 var(--acc-h))` |
| `--bg-row-foc` | `#eef0f3` |
| `--bd` | `#e3e5e9` |
| `--bd-soft` | `#ebedf0` |
| `--fg` | `#18191c` |
| `--fg-mut` | `#5b5e66` |
| `--fg-dim` | `#888b92` |
| `--fg-faint` | `#b6b8bd` |

### Accent + status

All defined in oklch. Default accent hue is 232°.

| Token | oklch | Hex (approx) | Use |
|---|---|---|---|
| `--acc` | `oklch(0.66 0.16 232)` | `#5e7cff` | Primary action, focus bar, selection bg seed |
| `--acc-soft` | `oklch(0.66 0.16 232 / 0.16)` | — | Selected bg in light mode, focus rings |
| `--acc-bd` | `oklch(0.66 0.16 232 / 0.42)` | — | Selected reason chip border |
| `--c-create` | `oklch(0.74 0.13 150)` | `#5fb87a` | CREATE action chip, high confidence, success |
| `--c-update` | `oklch(0.72 0.13 235)` | `#5da3df` | UPDATE action chip, medium confidence, SOP chip |
| `--c-complete` | `oklch(0.72 0.13 295)` | `#a982e6` | COMPLETE action chip |
| `--c-amb` | `oklch(0.78 0.13 85)` | `#d4be4d` | AMBIGUOUS action chip, conflict / warning |
| `--c-redhot` | `oklch(0.72 0.16 27)` | `#df6b53` | RedHot space, urgent, due today, vstale dot |
| `--c-border` | `oklch(0.78 0.12 70)` | `#d6a55a` | Borderline space chip |

Background tints are the same hue at `0.13–0.14` alpha (e.g. `--c-create-bg = oklch(0.74 0.13 150 / 0.13)`).

### Typography

- Family: `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif`.
- Mono (kbd pills): `ui-monospace, SFMono-Regular, Menlo, monospace`.
- Feature settings: `"cv11","ss01","ss03"` for OpenType variants when available.
- Tabular numerics on every number column (age, conf, counts, dates).

| Use | Size | Weight | Letter-spacing | Line-height |
|---|---|---|---|---|
| Page title (H1) | 18 | 600 | -0.01 | 1.45 |
| Detail title (H2) | 15 | 600 | -0.005 | 1.45 |
| Sub-tab | 12.5 | 400 | 0 | 1.45 |
| Row text | 12.5 | 400 | 0 | 1.45 |
| Row meta (age, conf, source) | 11.5 | 400 | 0 | 1.45 |
| Action chip | 10.5 | 600 | 0.05 | 1 |
| Section header | 10.5 | 500 | 0.06 / uppercase | 1 |
| Forecast number | 20 | 600 | -0.02 | 1.1 |
| Kbd pill | 10.5 | 600 | 0.02 | 1 |
| Toast / button | 12 | 400/500 | 0 | 1.45 |

### Spacing

8-point base. Common values used:

- Row padding: `0 14` (detail open), `0 16` (detail closed)
- Detail body: `14 18`, gap `18`
- Modal body: `14 18`, gap `10`
- Sticky bulk bar: `8 10` outer, `28px` button height
- Forecast strip: `10 16 12` outer, 6px gap between cards, `10 12` card padding

### Density tokens

| Density | `--row-h` | `--fs-row` | `--fs-meta` |
|---|---|---|---|
| compact (default) | 32px | 12.5px | 11.5px |
| cozy | 36px | 13px | 11.5px |
| comfy | 40px | 13.5px | 12px |

### Radius

- Chips: 9px (`height: 18px` → fully rounded)
- Buttons / inputs / cards: 6–8px
- Modal: 12px
- Sticky bulk bar: 10px
- Avatar: 50%

### Shadow

- Bulk bar: `0 12px 40px rgba(0,0,0,.5)`
- Modal: `0 24px 60px rgba(0,0,0,.5)`
- Toast: `0 8px 24px rgba(0,0,0,.4)`
- Help overlay: `0 16px 40px rgba(0,0,0,.45)`

---

## Assets

No images. All icons are inline SVG, drawn at 14×14 (rail) or 11×11 (source) with `currentColor` stroke/fill. Icons used: Slack-ish glyph, Doc, File, Inbox, Clock-stale, Snooze (Z+clock), Check, Warn (triangle), Search, X. Replace with the existing PWA's icon set if there is one — the inline SVGs in `triage.jsx` are a starting point, not final brand glyphs.

The `dot` in the brand mark is a CSS gradient (no asset).

---

## Files in this bundle

- `Triage.html` — the prototype shell. Loads React 18 / React DOM / Babel from unpkg, then `tweaks-panel.jsx` and `triage.jsx`. All CSS lives in a `<style>` block in the head.
- `triage.jsx` — the React implementation: data, helpers, icons, `<App>`, `<Row>`, `<Detail>`, `<DetailEdit>`, `<BulkBar>`, `<RejectModal>`, `<SnoozeModal>`, `<Help>`, plus the `SAMPLE` array (21 candidates) and `COMPLETED_TODAY` fixture.
- `tweaks-panel.jsx` — the floating Tweaks panel used to demo theme/density/accent. **Not part of the production design**; do not port it. It exists so the operator can flip dark/light and density during sign-off.
- `17-ui-ux-research.md` — the original research artifact this design implements. Read sections 6, 7, 8, 9a, 10, 13 before starting. The phasing table in §10 should drive your PR sequencing.

## Files in the existing PWA you'll touch

- `web/app.js` — replace `renderReviewItem` with `renderTriageRow`; add `renderTriageDetail`, `renderBulkBar`, `renderRejectModal`, `renderSnoozeModal`, `renderShortcutsOverlay`. Add the `state.triage` slice and a single keyboard handler attached at the top level.
- `web/styles.css` — add the token block. Replace the existing review-tab styles with the row / detail / bulk bar styles. Watch for collisions with existing `.row` / `.chip` selectors; namespace under `.triage` if needed.
- `web/index.html` — rename the "Review" tab to "Triage" and make it the default landing tab.
- `db/migrations/0008_snooze.sql` — new column: `ALTER TABLE review_items ADD COLUMN snoozed_until TIMESTAMPTZ NULL;` plus an index on `(status, snoozed_until)` for the Inbox query.
- `web/sw.js` — no changes expected.

## What's intentionally not in this handoff

- Tasks tab redesign (Phase UI-3+).
- Provenance-first Task detail (Phase UI-5).
- Activity tab + charts (Phases UI-6, UI-8).
- Saved views (Phase UI-3) beyond the four sub-tabs and forecast filter.
- Mobile / narrow-viewport drawer behavior (called out as a follow-up; see Detail pane open behavior above).
- Conflict-resolution sheet for bulk approve partial success (specced in §8 of the research artifact).
- The "Failed ingest" sub-view from §6 (held off; add as a 5th sub-tab once Activity-side ingest streaming is wired up).

Open questions that should be answered in operator session before merging:

- Selection persistence across sub-view switches (see Selection model).
- Whether `Esc` should also close an open detail pane after clearing selection / forecast / search.
- Whether the "completed today" sub-view should show rejected-today items, snoozed-today, or only approved-today.
