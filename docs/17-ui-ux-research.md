# 17 — UI/UX research artifact (pre-build)

> Generated 2026-05-09. Inputs: PWA codebase inventory (web/app.js
> 3.4K lines vanilla JS, 4 tabs, server-rendered HTML strings) +
> Codex comparable-product audit citing Linear / Asana / Things 3
> / OmniFocus / Todoist current docs. Companion file:
> `C:\Users\agbus\AppData\Local\Temp\codex_uiux_audit.out` — full
> Codex output.
>
> This doc is the input to **artifact synthesis** before any
> visual code change. Operator (Kyle) reacts after reading; we
> only build after Kyle has signed off on shape + phasing.

---

## 1. Why this exists

Operator's words 2026-05-08: *"the UI is very generic and basic."*
PWA shipped with chunk-4 review tab + chunk-12 SOP admin + chunk-13
notifications, prioritizing data correctness over visual polish.
That was right at v1; the system is now correct, observed, and
in real daily use — visual + interaction quality is the next gate.

Codex's prescribed sequence (2026-05-08 review): observation →
audit → artifact → wireframes → operator reaction → code. We're at
the artifact step.

---

## 2. Current PWA inventory

| File | Lines | Purpose |
|---|---|---|
| `web/index.html` | 27 | shell + script tags |
| `web/app.js` | 3431 | all rendering + state + API |
| `web/styles.css` | 1113 | all styles |
| `web/outbox.js` | 178 | offline write queue |
| `web/sw.js` | 342 | service worker (PWA + push) |

Vanilla JS, no framework, no build step. State held in a
top-level `state` object; renders are HTML-string concatenation
into `#root`. The unit of UI is a `render*` function — `renderTask`,
`renderReviewItem`, `renderSopItem`, etc. Mobile-first PWA;
manifest + service worker; push works on iOS.

### Top-level views (current 4-tab IA)

| Tab | Surfaces | Notes |
|---|---|---|
| **Tasks** | filterable card list | filters: business, status, owner. cards show summary + status pill + business pills + assignee + due + relative-time |
| **Review** | pending review_items | rows show action type (CREATE/UPDATE/COMPLETE/AMBIGUOUS), confidence, source. Click expands to candidate_facts + retrieved_candidates + validation_errors. Approve / Reject / Edit-then-approve buttons per item. **No batch ops, no keyboard, no snooze.** |
| **SOPs** | admin list + version edit + anchor scheduling | admin-only |
| **Settings** | Web Push subscription + digest opt-ins | low frequency of use |

---

## 3. Observed friction (today's session)

This is the qualitative side; needs operator-observation
follow-through (item 11 below) to weight properly.

| # | Friction | Where felt |
|---|---|---|
| F1 | Review queue requires per-item click → expand → approve. With 5–30 candidates/day, this is the slowest part of daily ops. | Review tab |
| F2 | No batch actions (approve N, reject N with shared reason, snooze N). | Review tab |
| F3 | No keyboard shortcuts. Mouse-only review flow. | All tabs |
| F4 | "Failed to fetch" surface when CF Access JWT expires. Hit twice today. Browser-side retry is silent; user has to hard-refresh or re-auth. | Globally |
| F5 | Filters work but feel like CRUD form, not saved-views / pivots. | Tasks + Review |
| F6 | Audit trail (`task_history` with full provenance, `task_field_versions`, `source_event_id` -> ingest_events -> raw Slack message) is *very strong* but only surfaces in task detail; not a product surface. | Task detail |
| F7 | No staleness affordance. A candidate sitting 3 days in review looks identical to one from 5 minutes ago. | Review tab |
| F8 | Generic visual treatment — bare divs, weak hierarchy, identical sizing on cards regardless of urgency. | Globally |

---

## 4. Comparable-product audit (full table from Codex)

Sources are 2025-2026 current docs, retrieved 2026-05-09 via web
search.

| Pattern | What it does | OpsMemory mapping | Cost |
|---|---|---|---|
| **Linear:Triage** | Team inbox for issues from Slack/support/integrations; accept, decline, duplicate, snooze with shortcuts `1/2/3/H`. | Closest model for `review_items`: every AI candidate enters a deliberate "before workflow" queue. | Med |
| **Linear:Select Issues** | `X`, shift-select, `Cmd/Ctrl+A`, command menu, bottom bulk-action bar. | Direct answer to 5–30 daily review items; batch approve/reject/snooze without inventing UX. | Med |
| **Linear:Inbox** | Notification list with quick search, read/unread, snooze, display options, `J/K`. | Review should behave like an operator inbox, not a CRUD table; pending/stale/snoozed/read states map cleanly. | Low-Med |
| **Asana:Inbox → Saved Tabs** | Inbox filters by person/project/mention/assignment/read, plus saved tabs. | Build saved review/task pivots: RedHot, Borderline, stale, high confidence, ambiguous, Kyle-as-reviewer. | Med |
| **Asana:Multi-home** | One task can live in multiple projects without duplication. | Mirrors `task_businesses` M2M; treat business pills as first-class location, not tags. | Low |
| **Asana:Task Activity Feed** | Task detail shows comments + collapsible history of creation, due, assignee, description changes. | OpsMemory has *stronger* provenance; surface `task_history`, `source_event_id`, per-field versions inline. | Med |
| **Things 3:Quick Find** | `Cmd+F` jumps to tasks, lists, areas, tags. | Add app-wide command/find: task, review item, SOP, ingest event, business, assignee, saved view. | Med |
| **Things 3:Today/Anytime/Someday** | Date-based attention buckets. | Add "Today" / "Anytime" / "Snoozed" / "Stale" review/task views instead of raw status filters. | Low-Med |
| **Things 3:Keyboard Selection** | Fast select, extend selection, complete, cancel, navigate. | Gives daily review a calm rhythm: select → approve → reject → edit → next. | Low |
| **OmniFocus:Forecast** | Date-tiled strip with overdue/today/future counts. | Top strip for review/task age: overdue, today, this week, snoozed, stale. | Low-Med |
| **OmniFocus:Review** | Literal Review mode: one project at a time, next/previous, mark reviewed, cadence. | "Candidate review focus mode": one review_item open, source + diff + approve/reject/edit, then next. | Med |
| **OmniFocus:Custom Perspectives** | Saved rule-based views (AND/OR/NOT). | Mature version of saved views; start with fixed presets before rule builder. | Med |
| **Todoist:Quick Add** | `Q` opens capture with inline grammar (`#project`, `@label`, `p1`, `!reminder`). | Manual fallback for task creation + edit-then-approve forms. | Med |
| **Todoist:Filters** | Custom queries: project, label, priority, assignment, date, AND/OR/NOT, favorites. | Named filters over business / status / owner / source / confidence / staleness. | Med |
| **Todoist:Keyboard Shortcuts** | `Q`, `/`, `Cmd/Ctrl+K`, `J/K`, `G then …`, `?`. | Best baseline for vanilla PWA: broad, discoverable, web-native. | Low |

**Bonus:** Slack:Later (In Progress / Archived / Completed
saved items), HEY:Screener/Imbox (decide once → route future
items from sender), Notion:AI Meeting Notes (transcript citations
+ action-item extraction).

---

## 5. Top 5 patterns ranked by (impact × low-cost)

1. **Linear-style Triage focus mode** — single candidate, source
   context, `1/2/3/H`, next item. Highest impact for daily ops.
2. **Linear bulk selection + sticky bulk bar** — `X`, shift-select,
   approve/reject/snooze N. Directly removes per-item friction.
3. **Asana / Todoist saved views** — "RedHot pending", "Borderline
   stale", "Ambiguous", "High confidence", "Failed ingest".
4. **Provenance-first task detail** — Asana activity feed style,
   but stronger: field diffs, mutation IDs, source event,
   linked llm_call.
5. **OmniFocus Forecast strip / staleness affordance** — date tiles
   adapted to "pending review age" + "due tasks." Surfaces F7.

---

## 6. IA proposal (the new tab structure)

Recommended: light restructure, not a rewrite.

| Now | Proposed | Why |
|---|---|---|
| Review | **Triage** (default tab) | Triage signals "before-workflow inbox", not "edit form." Sub-views: `Inbox`, `Stale`, `Snoozed`, `Completed today`, `Failed ingest`. |
| Tasks | **Tasks** (unchanged) | But starts with saved views: `Today`, `Blocked`, `Stale`, `RedHot`, `Borderline`, `No owner`. Filter form moves under a `+ Save view` button. |
| SOPs | **SOPs** (unchanged) | Already focused. |
| Settings | gear/account menu | Low daily use; doesn't need top-level real estate. |
| (none) | **Activity** (new) | Provenance-first surface: ingest events stream, task history, failed extractions, llm_call audit, daily cost. Makes the durable strength of OpsMemory's data model legible. |

**Default tab on app open:** `Triage` (operator's daily entry
point), not `Tasks`. Shifts the implicit workflow from "browse
my tasks" → "process the queue, then act."

---

## 7. Keyboard map (15 shortcuts; matches Linear / Things / Todoist)

| Shortcut | Action |
|---|---|
| `?` | Show shortcut overlay |
| `Cmd/Ctrl+K` | Command palette / Quick Find |
| `/` | Filter current list |
| `G` then `R` | Go to Triage |
| `G` then `T` | Go to Tasks |
| `G` then `S` | Go to SOPs |
| `J` / `K` (or `↑` / `↓`) | Move focus |
| `Enter` | Expand / open focused item |
| `Esc` | Collapse detail; clear selection/search |
| `X` | Toggle selection on focused row |
| `Shift+↑` / `Shift+↓` | Extend selection |
| `Cmd/Ctrl+A` | Select all visible |
| `1` | Approve focused/selected review item(s) |
| `2` | Reject focused/selected (prompts for reason) |
| `3` | Edit-then-approve focused item |
| `H` | Snooze focused/selected (prompts for date) |

These 15 cover ~95% of daily ops. Rest can come later.

---

## 8. Batch actions (the sticky bottom bar)

Pattern: **sticky bottom bar appears when ≥1 row selected.**

```
12 selected | Approve eligible (10) | Reject... | Snooze... | Assign reviewer | Clear
```

Operations:
- **Approve N** — applies via existing chunk-4 transactional
  apply path. Modal groups by action type:
  ```
  8 CREATE_TASK    -> apply
  3 UPDATE_TASK    -> apply
  1 AMBIGUOUS      -> skipped (must edit first)
  2 conflicts      -> require refresh
  ```
  Show partial-success + conflict rows after.
- **Reject N (shared reason)** — single textarea, applied to all.
- **Snooze N until date** — date picker + reason. Snoozed items
  hidden from main Inbox until date hits. Implement via a new
  `review_items.snoozed_until` column (chunk-4.5 schema bump).
- **Assign reviewer** — bulk-set `review_items.reviewer_user_id`
  (already tracked, just no UI).
- **Set business / category / priority** — bulk attribute edits
  before approve.

Snoozed items appear in the `Snoozed` sub-view of Triage.

---

## 9. Wireframes (markdown, from Codex)

### 9a. Triage (replaces Review)

```text
TRIAGE
[Inbox 18] [Stale 4] [Snoozed 3] [Completed today]       / search

Age   Action     Confidence   Source          Candidate                    Flags
3d    CREATE     0.91         #ops-redhot     Order cones for Mesa stand    RedHot  due Jun 1
1d    UPDATE     0.84         recap May 8     Change opener checklist       SOP?    conflict
2h    COMPLETE   0.96         #ops-border...  Submit SD permit packet       Borderline

[detail pane]
CREATE_TASK · 0.91 · pending 3d
Source: Slack #ops-redhot · May 6 · jump to raw event
Proposed facts: summary, due_at, business, assignee, priority
Retrieved candidates: 2 possible matches
Validation: none

[1 Approve] [3 Edit + approve] [2 Reject] [H Snooze]
```

### 9b. Task detail (provenance-first)

```text
Replace POS signs at Mesa stand            [Open] [High] [RedHot]
Owner: Kyle    Reviewer: Amy    Due: May 20    Last activity: 2h

Description
...

Businesses          Assignees           Dependencies         SOP anchors
RedHot              Kyle / assignee     Waiting on vendor    Store-opening v3

Provenance
May 9  10:42  summary changed         mutation m_182   source slack_991
May 9  10:42  assignee added Kyle     mutation m_182   source slack_991
May 8  16:10  task created            mutation m_177   source recap_044

Field versions
summary v4 | due_at v2 | priority v1 | businesses v3
```

### 9c. Tasks (saved views + Forecast strip)

```text
TASKS
[Today] [Blocked] [Stale] [No owner] [RedHot] [Borderline] [+ Save view]

Forecast:  Overdue 2 | Today 5 | This week 11 | No due 7 | Stale 4

Today
[ ] Submit AZ insurance packet         RedHot       Kyle       due today
[ ] Confirm SD tent delivery           Borderline   Kyle       blocked

Stale
[ ] Verify generator rental            RedHot       unowned    no activity 9d
```

---

## 10. Implementation phasing (proposed)

Ranked by lowest-cost-first, biggest-impact-first within tier.

### Phase UI-1 — keyboard + selection (1–2 days)

Cheapest, highest immediate operator win. No schema change.

- `J/K`/arrow navigation in Triage + Tasks list.
- `X` selection + shift-select + `Cmd+A`.
- `1/2/3/H` action shortcuts on focused / selected items.
- `?` overlay.
- `Cmd+K` palette stub (just navigation: Go to Triage / Tasks /
  SOPs). Real fuzzy search later.

### Phase UI-2 — Triage rename + sub-views + sticky bulk bar (2–3 days)

- Rename "Review" tab to "Triage."
- Add `Inbox` / `Stale` / `Snoozed` / `Completed today` /
  `Failed ingest` sub-views.
- Sticky bottom bar on selection ≥ 1.
- Approve N + Reject N (shared reason) — no schema change.
- Snooze N — needs `review_items.snoozed_until` column (schema
  bump 0008_snooze.sql + applied filter on the Inbox query).

### Phase UI-3 — saved views (Tasks + Triage) (1–2 days)

- Hardcoded preset list to start: `Today`, `Blocked`, `Stale`,
  `No owner`, `RedHot`, `Borderline`. Each is a fixed query.
- `+ Save view` button writes a `user_views` row (new schema:
  user_id, name, target_tab, query_json, sort_order). Show in
  saved-views list.
- Defer rule-builder UI; just textarea-edit the query JSON for
  now.

### Phase UI-4 — Forecast strip + staleness (1 day)

- Top strip on Tasks: `Overdue N | Today N | This week N |
  No due N | Stale N`. Each clickable → filters list.
- "Stale" = `last_activity_at < now() - 7 days` AND status='open'.
- Triage equivalent: `Pending 3d+ | Pending 7d+`.

### Phase UI-5 — provenance-first task detail (2–3 days)

- Inline `task_history` rows with mutation_id link → click to
  see all rows under that mutation.
- Inline `task_field_versions` line at bottom of detail.
- Source-event link on every history row → opens a side panel
  with the raw Slack message / meeting recap / file drop.
- Llm_call link if applicable → side panel with prompt + tokens
  + cost.

### Phase UI-6 — Activity tab (1 day)

New top-level tab: `Activity`. Streams of:
- Recent ingest_events (success / failed).
- Recent task_history mutations.
- Recent llm_calls with cumulative daily cost.
- Failed extractions to investigate.

Mostly rendering existing data. Becomes the "what happened
recently across all of OpsMemory" surface for the operator.

### Phase UI-7 — visual polish pass (2–3 days)

Now that structure is right, the actual visual design:
- Card hierarchy with size + emphasis based on staleness +
  priority.
- Color-tier the action types (CREATE green / UPDATE blue /
  COMPLETE purple / IGNORE gray / AMBIGUOUS yellow).
- Improve spacing, typography, density toggle.
- Dark-mode default; light-mode option.

Total for all 7 phases: ~10–15 days of focused PWA work.
UI-1 + UI-2 alone capture maybe 60% of the daily-ops win.

---

## 11. Operator-observation questions for Kyle

These need real session-watching to answer. Open until then.

1. **What's your daily Triage rhythm?** Once in the morning, or
   whenever a notification fires?
2. **How often do you batch-approve "5 of these are obviously
   fine, click approve"** vs. open each one to read carefully?
   Decides whether bulk-approve-without-reading needs a
   confirmation step.
3. **When you reject, do you ever write a unique reason per
   item, or is it always "wrong" / "duplicate" / "out of scope"?**
   Decides whether reject N with shared reason covers 100% of
   cases or just 80%.
4. **When does the staleness signal start mattering?** 1 day?
   3 days? Differs by source?
5. **Do you actually use the Tasks tab daily**, or is it more of
   "I've already seen everything in Triage so I rarely browse the
   open list"? Decides whether Tasks polish is high or low
   priority.
6. **Will Joanna review too?** Or admin-only forever? Adjusts
   the multi-reviewer assignment UI.
7. **Mobile vs desktop?** PWA works on both, but a mobile-first
   redesign would skip Cmd+K and lean on swipe. Browser-only?
   Phone-only? Both?
8. **Notifications: today** they fire on push when something
   needs attention. Should the in-app surface match that
   ranking, or stay chronological?

Run through these *while watching Kyle / Joanna actually use
the PWA for one normal Triage session.* Don't ask out of
context — answers will be wrong.

---

## 12. Open questions for next-session decision

- Build phasing UI-1 first (cheap keyboard+selection win) **OR**
  start with UI-2 (Triage rename + bulk bar) for more visible
  shape change?
- Schema bump for `review_items.snoozed_until` — minor but
  blocks UI-2 snooze actions. Is this enough to gate UI-2 or
  do snooze last?
- Vanilla JS staying, or pull in a small framework (Preact /
  Lit / Alpine) once rendering complexity grows? At 3.4K lines
  of HTML strings, the boundary is closer than it looks.
- Cmd+K palette: build it as a navigation-only first cut (no
  fuzzy search across data), or wait until we have better
  search infra?
