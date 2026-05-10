'use strict';

// OpsMemory Chunk 2 PWA: read-only task dashboard.
// State machine:
//   1. Load /whoami → render header with user + role
//   2. Load /v1/businesses → populate business filter
//   3. Load /v1/tasks (with filters) → render task list
//   4. Click a task → fetch /v1/tasks/{id} → render detail
// All requests are same-origin; Cloudflare Access + the Postgres-backed
// users/businesses tables enforce visibility. Owner accounts only see
// tasks for their business memberships.

const state = {
  principal: null,
  businesses: [],
  tasks: [],
  total: 0,
  filters: {
    status: 'open',     // 'open' | 'done' | 'all'
    business: 'all',    // 'all' | <slug>
  },
  expandedTaskId: null,
  expandedTaskDetail: null,
  view: 'tasks',                // 'tasks' | 'review' | 'sops' (admin) | 'settings' (any user)
  review: {
    items: [],
    total: 0,
    statusFilter: 'pending,needs_changes',
    expandedId: null,
    expandedDetail: null,
    pendingAction: null,        // 'approve' | 'reject' | null while a write is in flight
    actionError: null,
    // Phase UI-1 (2026-05-09): keyboard + selection state.
    // focusedIndex tracks which row in items[] has the focus
    // outline (J/K + arrow keys move it). selectedIds is the set
    // of ids the operator has marked with X / shift-arrow /
    // Cmd+A; the 1/2 action shortcuts apply to the focused row
    // when the set is empty, or to every selected row when ≥1.
    focusedIndex: null,         // null | int index into items_visible[]
    selectedIds: new Set(),     // Set<reviewItemId>
    bulkInProgress: false,      // true while a multi-row 1/2 is iterating
    // Phase UI-2 (2026-05-09): Triage redesign state.
    subview: 'inbox',                        // 'inbox' | 'stale' | 'snoozed' | 'completed'
    forecastFilter: null,                    // null | 'fresh' | 'warm' | 'stale' | 'vstale' | 'urgent'
    searchQuery: '',                         // current /-search filter text
    detailOpen: true,                        // detail pane visible at desktop widths
    rejectModal: null,                       // null | { ids, preset, reasonText }
    snoozeModal: null,                       // null | { ids, selectedKey, customIso, reasonText }
    toast: null,                             // null | { msg, kind }
    items_visible: [],                       // computed at render time; filtered subset focused/selected indexes into
    editingId: null,                         // null | review_item id currently in inline edit mode
    editDraft: null,                         // { action, summary, due_at, category, dependency_text, completion_note, edit_reason }
    editSaving: false,
    editError: null,
  },
  // Phase UI-2B2: Quick Add compose modal — Slack-style ad-hoc task
  // capture. Lives at the top level (not under review.) so Q can
  // open it from any view, including Tasks / SOPs / Settings.
  quickAdd: {
    open: false,
    summary: '',
    businessSlug: '',
    dueAtLocal: '',
    description: '',
    kind: 'task',                            // 'task' | 'event'
    submitting: false,
    error: null,
    // B3-2 additions
    ownerUserId: '',                         // '' = unassigned (default to creator at submit)
    members: [],                             // [{id, display_name, role}]
    membersLoading: false,
    dedup: null,                             // null | { candidates: [...], previewToken: "..." }
  },
  // SOPs view (chunk 7 step 4 — admin-only).
  sops: {
    items: [],
    total: 0,
    statusFilter: 'active',     // 'active' | 'archived' | 'all'
    businessFilter: 'all',      // 'all' | <slug>
    expandedId: null,           // SOP id whose detail is loaded
    expandedDetail: null,       // { sop, versions[] }
    selectedVersionNo: null,    // version_no whose templates are loaded
    selectedVersionDetail: null, // { version, template_tasks[] }
    loadError: null,
    // UI 2/3: inline authoring. When non-null, the SOP detail
    // renders an editable template table for the editing target.
    // Scoped by BOTH sopId and versionNo (Codex chunk-7-step4-ui2
    // blocker — without sopId scoping, expanding another SOP and
    // hitting Save would clobber the new SOP with the prior SOP's
    // template buffer).
    editing: null,              // null | { sopId, versionNo, templates, saving, saveError, publishError, pending, dirty }
    // UI 3/3: collapsible Create SOP form.
    creating: null,             // null | { businessSlug, name, description, pending, error }
  },
  // UI 3/3: Anchors section below the SOP list. Independent filters
  // per Codex (don't couple to SOP list filters).
  anchors: {
    items: [],
    total: 0,
    statusFilter: 'scheduled',  // 'scheduled' | 'fired' | 'cancelled' | 'all'
    businessFilter: 'all',
    expanded: false,            // anchors section visibility (collapsed by default)
    scheduling: null,           // null | { businessSlug, sopId, kind, scheduledFor, notes, sopOptions, pending, error }
    fireResult: null,           // null | { anchorId, reviewItemsCreated, instanceId }
    loadError: null,
  },
  // Sync indicator state. The header pill reads from these.
  sync: {
    online: (typeof navigator !== 'undefined') ? navigator.onLine : true,
    pending: 0,                 // count of mutations waiting to apply
    conflict: 0,                // count of mutations in 'conflict' state
    fromCache: false,            // last /v1/tasks render came from SW cache
  },
  // Per-task optimistic patches keyed by task_id. Used to overlay
  // pending in-flight mutations on the rendered task. Cleared when a
  // mutation reaches a terminal state.
  optimistic: {},
  // Phase UI-3: dashboard tiles state. Loaded on Tasks tab open and
  // refreshed when the business filter changes. Shape mirrors the
  // GET /v1/dashboard/summary response.
  dashboard: {
    loaded: false,
    loading: false,
    loadError: null,
    totals: { open: 0, done_7d: 0, done_30d: 0 },
    open_aging: { today: 0, '1_3d': 0, '3_7d': 0, '7d_plus': 0 },
    by_business: [],
    spark_daily_done: [],
    forBusiness: 'all',          // 'all' | <slug>; refresh when this drifts from filters.business
  },
  // Chunk 10 step 3 sub-(a): Settings tab (read-only). Displays
  // notification prefs, web-push subscriptions, and the server's
  // VAPID config status. NO permission prompts, NO subscribe, NO
  // PATCH from this commit — those land in sub-(b)/(c).
  settings: {
    prefs: [],
    subscriptions: [],
    vapidPublicKey: null,         // null = server not configured (or 503)
    permissionState: 'default',   // 'default' | 'granted' | 'denied' | 'unsupported'
    pushApiAvailable: null,       // boolean | null (null = not yet probed)
    serviceWorkerReady: false,    // resolved with timeout in refreshSettings
    browserSubscription: null,    // PushSubscription read-only
    loaded: false,                // first refresh has completed
    loading: false,               // a refresh is in flight
    loadError: null,              // inline error pill (does NOT replace shell)
    // Sub-(b) commit 1: per-channel editor drafts. Codex chunk-10-
    // step3b1 (3): keyed map of {channel -> draft}, no global
    // pendingChannel. Per-draft pending + save-handler guard is
    // enough.
    editing: {},                  // { [channel]: draft } — see _seedSettingsDraft
    // Sub-(b) commit 2: per-device revoke. Codex chunk-10-step3b2
    // plan-review: per-row pending map; revoke errors display as
    // a single inline pill near the subscription table.
    subscriptionRevokes: {},      // { [subscriptionId]: true } when DELETE is in flight
    subscriptionRevokeError: null,
    // Sub-(c)+ commit 4: Send test push per device. Per-row state
    // so multiple devices can be tested in parallel.
    subscriptionTests: {},        // { [subscriptionId]: { pending, status, http, code, detail } }
    // Sub-(b) commit 3: Enable Web Push (subscribe) flow. Single
    // global pending+error pair per Codex chunk-10-step3b2 gate-2
    // (plan for sub-(b)/3).
    subscriptionCreatePending: false,
    subscriptionCreateError: null,
  },
};

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------

async function api(path, opts) {
  const init = {
    method: (opts && opts.method) || 'GET',
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' },
  };
  if (opts && opts.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let body = null;
  try { body = await res.json(); } catch { /* may be empty */ }
  // Pick up the SW's stale-cache marker for /v1/tasks reads.
  if (res.headers && res.headers.get('X-OpsMemory-From-Cache') === '1') {
    state.sync.fromCache = true;
  }
  if (res.status === 401) throw { kind: 'auth', message: 'Session expired. Refresh the page.', status: 401 };
  if (res.status === 403) throw { kind: 'forbidden', message: 'You don\'t have permission for that.', status: 403 };
  if (res.status === 404) throw { kind: 'not_found', message: 'Not found.', body, status: 404 };
  if (res.status === 409) throw { kind: 'conflict', message: 'Conflict — the underlying task changed. Re-check and retry.', body, status: 409 };
  if (res.status === 422) throw { kind: 'validation', message: 'Validation failed.', body, status: 422 };
  if (res.status === 501) throw { kind: 'not_implemented', message: 'Not implemented yet.', body, status: 501 };
  if (!res.ok) throw { kind: 'server', message: `Server returned ${res.status}.`, body, status: res.status };
  return body;
}

// Codex chunk-6-close: SW intentionally does not cache /whoami or
// /v1/businesses (auth-scoped), but a true airplane-mode reload would
// then render the error UI before the outbox could restore optimistic
// state. localStorage persistence of the last-known principal +
// businesses lets the PWA boot offline against a recently-known shape.
const _LS_PRINCIPAL_KEY = 'opsmemory.principal.v1';
const _LS_BUSINESSES_KEY = 'opsmemory.businesses.v1';

function _persistPrincipal(p) {
  try { localStorage.setItem(_LS_PRINCIPAL_KEY, JSON.stringify(p)); }
  catch (e) { /* quota / private mode — ignore */ }
}
function _readPersistedPrincipal() {
  try {
    const raw = localStorage.getItem(_LS_PRINCIPAL_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function _persistBusinesses(b) {
  try { localStorage.setItem(_LS_BUSINESSES_KEY, JSON.stringify(b || [])); }
  catch (e) { /* ignore */ }
}
function _readPersistedBusinesses() {
  try {
    const raw = localStorage.getItem(_LS_BUSINESSES_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

async function loadPrincipal() {
  const p = await api('/whoami');
  _persistPrincipal(p);
  return p;
}

async function loadBusinesses() {
  const data = await api('/v1/businesses');
  const businesses = data.businesses || [];
  _persistBusinesses(businesses);
  return businesses;
}

async function loadTasks(filters) {
  const params = new URLSearchParams();
  if (filters.status && filters.status !== 'all') params.set('status', filters.status);
  if (filters.business && filters.business !== 'all') params.set('business_slug', filters.business);
  params.set('limit', '200');
  const data = await api('/v1/tasks?' + params.toString());
  return { tasks: data.tasks || [], total: data.total || 0 };
}

async function loadTaskDetail(taskId) {
  const data = await api('/v1/tasks/' + encodeURIComponent(taskId));
  return data.task;
}

// ---------- Review queue ----------

async function loadReviewItems(statusFilter) {
  const params = new URLSearchParams();
  if (statusFilter) params.set('status', statusFilter);
  // Phase UI-2B1: PWA does sub-tab filtering client-side
  // (Inbox / Stale / Snoozed are all subsets of pending). The API
  // default is `snoozed=exclude`; ask for `all` so the Snoozed
  // sub-tab has data without a second request.
  params.set('snoozed', 'all');
  params.set('limit', '100');
  const data = await api('/v1/review?' + params.toString());
  return { items: data.items || [], total: data.total || 0 };
}

async function loadReviewDetail(reviewId) {
  return await api('/v1/review/' + encodeURIComponent(reviewId));
}

async function approveReview(reviewId) {
  return await api('/v1/review/' + encodeURIComponent(reviewId) + '/approve',
                   { method: 'POST', body: {} });
}

async function rejectReview(reviewId, reason) {
  const body = reason ? { reason } : {};
  return await api('/v1/review/' + encodeURIComponent(reviewId) + '/reject',
                   { method: 'POST', body });
}

// ---------- SOPs (chunk 7 step 4 — admin-only browse) ----------

async function loadSops(filters) {
  const params = new URLSearchParams();
  if (filters.businessFilter && filters.businessFilter !== 'all') {
    params.set('business_slug', filters.businessFilter);
  }
  if (filters.statusFilter && filters.statusFilter !== 'all') {
    params.set('status', filters.statusFilter);
  }
  params.set('limit', '100');
  const data = await api('/v1/sops?' + params.toString());
  return { items: data.items || [], total: data.total || 0 };
}

async function loadSopDetail(sopId) {
  return await api('/v1/sops/' + encodeURIComponent(sopId));
}

async function loadSopVersionDetail(sopId, versionNo) {
  return await api('/v1/sops/' + encodeURIComponent(sopId) +
                    '/versions/' + encodeURIComponent(versionNo));
}

async function createSopDraftVersion(sopId, body) {
  return await api('/v1/sops/' + encodeURIComponent(sopId) + '/versions',
                   { method: 'POST', body: body || {} });
}

async function saveSopTemplates(sopId, versionNo, templates) {
  return await api('/v1/sops/' + encodeURIComponent(sopId) +
                    '/versions/' + encodeURIComponent(versionNo) + '/templates',
                   { method: 'PATCH', body: { templates } });
}

async function publishSopVersion(sopId, versionNo, body) {
  return await api('/v1/sops/' + encodeURIComponent(sopId) +
                    '/versions/' + encodeURIComponent(versionNo) + '/publish',
                   { method: 'POST', body: body || {} });
}

// ---------- Create SOP / Anchors / Fire (chunk 7 step 4 UI 3/3) ----------

async function createSop(body) {
  return await api('/v1/sops', { method: 'POST', body });
}

async function loadAnchors(filters) {
  const params = new URLSearchParams();
  if (filters.businessFilter && filters.businessFilter !== 'all') {
    params.set('business_slug', filters.businessFilter);
  }
  if (filters.statusFilter && filters.statusFilter !== 'all') {
    params.set('state', filters.statusFilter);
  }
  params.set('limit', '100');
  const data = await api('/v1/anchor_events?' + params.toString());
  return { items: data.items || [], total: data.total || 0 };
}

async function createAnchor(body) {
  return await api('/v1/anchor_events', { method: 'POST', body });
}

async function fireAnchor(anchorId) {
  return await api('/v1/anchor_events/' + encodeURIComponent(anchorId) + '/fire',
                   { method: 'POST', body: {} });
}

async function loadActiveSopsForBusiness(businessSlug) {
  // Codex chunk-7-step4-ui2 plan note: don't blindly source the SOP
  // dropdown from current state.sops.items (current filters can omit
  // valid active SOPs). Fetch a fresh active-only list scoped to the
  // selected business.
  const params = new URLSearchParams();
  params.set('status', 'active');
  if (businessSlug) params.set('business_slug', businessSlug);
  params.set('limit', '200');
  const data = await api('/v1/sops?' + params.toString());
  return data.items || [];
}

// ---------- Client mutations (Chunk 6 step 1 contract + step 3 outbox) ----------

async function postToggleDone(taskId, body) {
  return await api('/v1/tasks/' + encodeURIComponent(taskId) + '/toggle_done',
                   { method: 'POST', body });
}

async function refetchTask(taskId) {
  // Used by the conflict-recovery path to reload the canonical state
  // from the server after a 409. SW serves /v1/tasks list cached
  // (network-first), but /v1/tasks/{id} is network-only.
  const data = await api('/v1/tasks/' + encodeURIComponent(taskId));
  return data.task;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, function (c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}

function fmtDate(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch { return iso; }
}

// MT-2 (2026-05-10): platform admin gate. NO legacy 'admin' alias —
// migration 0023 must apply before this binary deploys, otherwise
// pre-migration rows still tagged 'admin' would leak platform-wide
// visibility (Codex MT-2 blocker).
function _isPlatformAdmin(p) {
  if (!p) return false;
  return p.role === 'platform_admin';
}

function fmtRelative(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const diffMs = Date.now() - d.getTime();
    const diffMin = Math.round(diffMs / 60000);
    if (diffMin < 1) return 'just now';
    if (diffMin < 60) return diffMin + 'm ago';
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return diffHr + 'h ago';
    const diffDay = Math.round(diffHr / 24);
    if (diffDay < 30) return diffDay + 'd ago';
    return fmtDate(iso);
  } catch { return iso; }
}

function renderSyncBadge() {
  const s = state.sync;
  if (!s.online) {
    const extra = s.pending ? ` (${s.pending} pending)` : '';
    return `<span class="sync-badge offline">offline${escapeHtml(extra)}</span>`;
  }
  if (s.conflict > 0) {
    return `<span class="sync-badge conflict">${s.conflict} conflict</span>`;
  }
  if (s.pending > 0) {
    return `<span class="sync-badge pending">${s.pending} pending</span>`;
  }
  if (s.fromCache) {
    return `<span class="sync-badge stale">cached</span>`;
  }
  return `<span class="sync-badge synced">synced</span>`;
}

function renderHeader() {
  const p = state.principal;
  if (!p) return '';
  const businesses = (p.businesses || [])
    .map(b => `<span class="biz-pill">${escapeHtml(b.name)}</span>`)
    .join(' ');
  const isAdmin = _isPlatformAdmin(p);
  // Codex chunk-10-step3a: Settings is per-user, gated on
  // principal_type==='user' (the backend's _require_user). Don't
  // reuse the admin role check.
  const isUserPrincipal = p.principal_type === 'user';
  const tabs = `
    <div class="view-tabs">
      <button class="view-tab${state.view === 'tasks' ? ' active' : ''}" data-view="tasks">Tasks</button>
      ${isUserPrincipal
        ? `<button class="view-tab${state.view === 'settings' ? ' active' : ''}" data-view="settings">Settings</button>`
        : ''}
      ${isAdmin
        ? `<button class="view-tab${state.view === 'review' ? ' active' : ''}" data-view="review">Triage</button>`
        : ''}
      ${isAdmin
        ? `<button class="view-tab${state.view === 'sops' ? ' active' : ''}" data-view="sops">SOPs</button>`
        : ''}
      <span class="sync-slot">${renderSyncBadge()}</span>
    </div>
  `;
  return `
    <div class="principal">
      <strong>${escapeHtml(p.display_name)}</strong>
      <span class="role-pill">${escapeHtml(p.role)}</span>
      <div class="email">${escapeHtml(p.email || '')}</div>
      <div class="biz-list">${businesses}</div>
    </div>
    ${tabs}
  `;
}

function renderDashboardTiles() {
  const d = state.dashboard;
  // First load: skeletons. Refresh-while-loaded: keep the prior
  // numbers visible (no flicker).
  if (!d.loaded && d.loading) {
    return `<div class="tg-dash"><div class="tg-dash-skel">Loading dashboard…</div></div>`;
  }
  if (d.loadError && !d.loaded) {
    return `<div class="tg-dash"><div class="tg-dash-err">⚠ ${escapeHtml(d.loadError)}</div></div>`;
  }
  const t = d.totals || {};
  const a = d.open_aging || {};
  const totalAging = (a.today || 0) + (a['1_3d'] || 0) + (a['3_7d'] || 0) + (a['7d_plus'] || 0);
  // Stacked aging bar segments. When all 0, render a faint full-width "0".
  function pct(n) {
    if (!totalAging) return 0;
    return Math.round((n / totalAging) * 100);
  }
  const seg = [
    { key: 'today',   cls: 'fresh',  n: a.today || 0 },
    { key: '1_3d',    cls: 'warm',   n: a['1_3d'] || 0 },
    { key: '3_7d',    cls: 'stale',  n: a['3_7d'] || 0 },
    { key: '7d_plus', cls: 'vstale', n: a['7d_plus'] || 0 },
  ];
  const agingBar = totalAging === 0
    ? `<div class="tg-dash-aging-bar empty"><span>nothing open</span></div>`
    : `<div class="tg-dash-aging-bar">
         ${seg.map(s => s.n
            ? `<span class="seg ${s.cls}" style="width:${pct(s.n)}%" title="${s.n} ${s.key}">${s.n}</span>`
            : '').join('')}
       </div>`;
  // Sparkline (14d daily done).
  const sparkSvg = renderSparkline(d.spark_daily_done || []);
  // Per-business open count tile (only when more than one biz visible).
  const bizRows = (d.by_business || []).filter(b => b.open > 0 || (d.by_business.length <= 4));
  const maxBizOpen = bizRows.reduce((m, b) => Math.max(m, b.open || 0), 1);
  const bizList = bizRows.length > 0 ? bizRows.map(b => `
    <div class="tg-dash-bizrow">
      <span class="biz-name">${escapeHtml(b.name)}</span>
      <span class="biz-bar"><span class="fill" style="width:${Math.round((b.open / maxBizOpen) * 100)}%"></span></span>
      <span class="biz-count">${b.open}</span>
    </div>
  `).join('') : `<div class="tg-dash-empty">No open tasks anywhere.</div>`;

  return `
    <div class="tg-dash">
      <div class="tg-dash-tile">
        <div class="tg-dash-lbl">Open</div>
        <div class="tg-dash-num">${t.open || 0}</div>
        <div class="tg-dash-sub">${t.done_7d || 0} done in 7d · ${t.done_30d || 0} in 30d</div>
      </div>
      <div class="tg-dash-tile">
        <div class="tg-dash-lbl">Open by age</div>
        ${agingBar}
        <div class="tg-dash-sub">today · 1–3d · 3–7d · 7d+</div>
      </div>
      <div class="tg-dash-tile">
        <div class="tg-dash-lbl">Daily completed (14d)</div>
        ${sparkSvg}
        <div class="tg-dash-sub">${t.done_7d || 0} this past week</div>
      </div>
      <div class="tg-dash-tile tg-dash-biz">
        <div class="tg-dash-lbl">Open by business</div>
        <div class="tg-dash-bizlist">${bizList}</div>
      </div>
    </div>
  `;
}

function renderSparkline(points) {
  if (!points || points.length === 0) return '<svg class="tg-spark" viewBox="0 0 100 24"></svg>';
  const w = 100;
  const h = 24;
  const max = Math.max(1, ...points.map(p => p.count || 0));
  const stepX = points.length > 1 ? w / (points.length - 1) : w;
  const coords = points.map((p, i) => {
    const x = i * stepX;
    const y = h - 2 - ((p.count || 0) / max) * (h - 4);
    return [x.toFixed(2), y.toFixed(2)];
  });
  const path = coords.map((c, i) => `${i === 0 ? 'M' : 'L'} ${c[0]} ${c[1]}`).join(' ');
  // Area fill below the line for a softer look.
  const areaPath = `${path} L ${(points.length - 1) * stepX} ${h - 1} L 0 ${h - 1} Z`;
  return `
    <svg class="tg-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
      <path d="${areaPath}" fill="var(--tg-acc-soft)" />
      <path d="${path}" fill="none" stroke="var(--tg-acc)" stroke-width="1.5" />
    </svg>
  `;
}

function renderFilters() {
  const businessOptions = [
    `<option value="all"${state.filters.business === 'all' ? ' selected' : ''}>All businesses</option>`,
    ...state.businesses.map(b =>
      `<option value="${escapeHtml(b.slug)}"${state.filters.business === b.slug ? ' selected' : ''}>${escapeHtml(b.name)}</option>`
    ),
  ].join('');
  return `
    <div class="filters">
      <div class="status-tabs">
        <button class="tab${state.filters.status === 'open' ? ' active' : ''}" data-status="open">Open</button>
        <button class="tab${state.filters.status === 'done' ? ' active' : ''}" data-status="done">Done</button>
        <button class="tab${state.filters.status === 'all' ? ' active' : ''}" data-status="all">All</button>
      </div>
      <select class="business-filter" id="business-filter">${businessOptions}</select>
    </div>
  `;
}

function renderTask(task) {
  const view = effectiveTask(task);
  const expanded = state.expandedTaskId === view.id;
  const detail = expanded ? state.expandedTaskDetail : null;
  const optimistic = !!state.optimistic[view.id];
  const conflictMutation = (state.outboxByTask && state.outboxByTask[view.id])
    ? state.outboxByTask[view.id].conflict : null;

  const dueLine = view.due_at
    ? `<div class="task-due">due ${escapeHtml(fmtDate(view.due_at))}</div>`
    : '';
  const depLine = view.dependency_text
    ? `<div class="task-dep">⏸ waiting on ${escapeHtml(view.dependency_text)}</div>`
    : '';
  const optimisticBadge = optimistic
    ? `<span class="opt-badge">syncing…</span>`
    : '';
  const conflictBadge = conflictMutation
    ? `<span class="conflict-badge">conflict</span>`
    : '';

  let detailBlock = '';
  if (expanded && detail) {
    const assignees = (detail.assignees || [])
      .map(a => `<li>${escapeHtml(a.display_name)} <span class="role-pill small">${escapeHtml(a.task_role)}</span></li>`)
      .join('');
    const businesses = (detail.businesses || [])
      .map(b => `<span class="biz-pill">${escapeHtml(b.name)}</span>`)
      .join(' ');
    const fieldVersionsList = Object.entries(detail.field_versions || {})
      .map(([k, v]) => `<code>${escapeHtml(k)}: v${v}</code>`)
      .join(' ');
    const toggleLabel = view.status === 'done' ? 'Reopen' : 'Mark done';
    const toggleClass = view.status === 'done' ? 'task-reopen' : 'task-complete';
    const toggleDisabled = optimistic ? 'disabled' : '';
    const conflictBlock = conflictMutation
      ? `<div class="task-conflict">
           <div class="conflict-msg">
             Conflict on last change: <code>${escapeHtml(conflictMutation.last_error || 'task_version_moved')}</code>
           </div>
           <button class="conflict-retry" data-key="${escapeHtml(conflictMutation.idempotency_key)}">Retry from current</button>
           <button class="conflict-discard" data-key="${escapeHtml(conflictMutation.idempotency_key)}">Discard</button>
         </div>`
      : '';
    detailBlock = `
      <div class="task-detail">
        ${detail.description ? `<div class="task-description">${escapeHtml(detail.description)}</div>` : ''}
        <div class="task-section"><h3>Assignees</h3>
          <ul>${assignees || '<li class="muted">(none)</li>'}</ul>
        </div>
        <div class="task-section"><h3>Businesses</h3>
          <div>${businesses || '<span class="muted">(none)</span>'}</div>
        </div>
        <div class="task-section"><h3>Field versions</h3>
          <div class="field-versions">${fieldVersionsList || '<span class="muted">(none)</span>'}</div>
        </div>
        <div class="task-section task-meta">
          <div>Version: <code>v${detail.version}</code></div>
          <div>Last activity: ${escapeHtml(fmtRelative(detail.last_activity_at))}</div>
          <div>Created: ${escapeHtml(fmtDate(detail.created_at))}</div>
          ${detail.completed_at ? `<div>Completed: ${escapeHtml(fmtDate(detail.completed_at))}</div>` : ''}
        </div>
        ${conflictBlock}
        <div class="task-actions">
          <button class="${toggleClass}" data-task-id="${escapeHtml(view.id)}" ${toggleDisabled}>
            ${toggleLabel}
          </button>
        </div>
      </div>
    `;
  }

  return `
    <li class="task ${view.status}${expanded ? ' expanded' : ''}${optimistic ? ' optimistic' : ''}${conflictMutation ? ' has-conflict' : ''}" data-task-id="${escapeHtml(view.id)}">
      <div class="task-header">
        <div class="task-summary">${escapeHtml(view.summary)}</div>
        ${optimisticBadge}
        ${conflictBadge}
        <div class="task-status-pill ${view.status}">${escapeHtml(view.status)}</div>
      </div>
      ${dueLine}
      ${depLine}
      ${detailBlock}
    </li>
  `;
}

function renderTaskList() {
  if (!state.tasks.length) {
    return `<div class="empty-state">No tasks match the current filters.</div>`;
  }
  return `
    <div class="task-count">${state.total} task${state.total === 1 ? '' : 's'}</div>
    <ul class="task-list">${state.tasks.map(renderTask).join('')}</ul>
  `;
}

// =====================================================================
// Phase UI-2 (2026-05-09) — Triage redesign.
//
// renderTriageView replaces the old renderReviewList as the rendered
// body for state.view === 'review'. It shows: title + sub-tabs +
// forecast strip + list (with new row layout) + detail pane (right
// at desktop, drawer on mobile) + sticky bulk bar.
//
// Sub-views (Inbox / Stale / Snoozed / Completed today) are computed
// client-side by filtering state.review.items, with counts shown in
// the tab badges. The Snoozed sub-view renders nothing meaningful
// until the 0008 schema bump lands; the tab is shown as a placeholder
// with `0`.
//
// Triage detail pane mirrors the renderReviewItem detail content
// but in the sidebar layout. Edit-then-approve (B4) and Snooze (B1)
// are wired live; this comment used to flag them as placeholders.
// =====================================================================

const TG = {
  // Reason presets shown in the reject modal.
  rejectPresets: ['Wrong', 'Duplicate', 'Out of scope', 'Already handled', 'Spam'],
  // Threshold tier boundaries (days).
  staleDays: 3,
  vstaleDays: 7,
  // Toast auto-dismiss (ms).
  toastMs: 1800,
};

function tgAgeMinutes(item) {
  if (!item || !item.created_at) return 0;
  const t = Date.parse(item.created_at);
  if (isNaN(t)) return 0;
  return Math.max(0, Math.floor((Date.now() - t) / 60000));
}

function tgAgeStr(min) {
  if (min < 60) return `${min}m`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d`;
}

function tgConfTier(c) {
  if (c == null) return 'low';
  if (c >= 0.85) return 'high';
  if (c >= 0.70) return 'med';
  if (c >= 0.55) return 'low';
  return 'vlow';
}

function tgActionClass(action) {
  const a = (action || '').toUpperCase();
  if (a === 'CREATE_TASK') return 'create';
  if (a === 'UPDATE_TASK') return 'update';
  if (a === 'COMPLETE_TASK') return 'complete';
  if (a === 'AMBIGUOUS') return 'amb';
  return 'ignore';
}

function tgActionLabel(action) {
  const a = (action || '').toUpperCase();
  if (a === 'CREATE_TASK') return 'CREATE';
  if (a === 'UPDATE_TASK') return 'UPDATE';
  if (a === 'COMPLETE_TASK') return 'COMPLETE';
  if (a === 'AMBIGUOUS') return 'Ambig.';
  return 'IGNORE';
}

// "Currently snoozed" predicate. snoozed_until comes from the API
// as an ISO string OR null. A past timestamp means the snooze has
// expired and the item should re-enter Inbox without server help.
function tgIsSnoozed(item) {
  if (!item || !item.snoozed_until) return false;
  const t = Date.parse(item.snoozed_until);
  if (Number.isNaN(t)) return false;
  return t > Date.now();
}

function tgSubviewItems() {
  const items = state.review.items || [];
  const sv = state.review.subview || 'inbox';
  const open = i => (i.status === 'pending' || i.status === 'needs_changes');
  if (sv === 'inbox') {
    return items.filter(i => open(i) && !tgIsSnoozed(i));
  }
  if (sv === 'stale') {
    return items.filter(i => {
      if (!open(i)) return false;
      if (tgIsSnoozed(i)) return false;
      const ageDays = tgAgeMinutes(i) / (60 * 24);
      return ageDays >= TG.staleDays;
    });
  }
  if (sv === 'snoozed') {
    return items.filter(i => open(i) && tgIsSnoozed(i));
  }
  if (sv === 'completed') {
    return items.filter(i => i.status === 'approved' || i.status === 'rejected');
  }
  return items;
}

function tgSubviewCounts() {
  const items = state.review.items || [];
  const open = i => (i.status === 'pending' || i.status === 'needs_changes');
  const inbox = items.filter(i => open(i) && !tgIsSnoozed(i)).length;
  const stale = items.filter(i => {
    if (!open(i)) return false;
    if (tgIsSnoozed(i)) return false;
    const ageDays = tgAgeMinutes(i) / (60 * 24);
    return ageDays >= TG.staleDays;
  }).length;
  const snoozed = items.filter(i => open(i) && tgIsSnoozed(i)).length;
  const completed = items.filter(i => i.status === 'approved' || i.status === 'rejected').length;
  return { inbox, stale, snoozed, completed };
}

function tgForecastCounts() {
  const items = state.review.items.filter(i =>
    i.status === 'pending' || i.status === 'needs_changes'
  );
  let fresh = 0, warm = 0, stale = 0, vstale = 0, urgent = 0;
  for (const i of items) {
    const ageDays = tgAgeMinutes(i) / (60 * 24);
    if (ageDays < 1) fresh++;
    else if (ageDays < TG.staleDays) warm++;
    else if (ageDays < TG.vstaleDays) stale++;
    else vstale++;
    // urgent: explicit ambiguous OR confidence below 0.55
    if ((i.proposed_action || '') === 'AMBIGUOUS' ||
        (i.confidence != null && Number(i.confidence) < 0.55)) {
      urgent++;
    }
  }
  return { fresh, warm, stale, vstale, urgent };
}

function tgRenderSpark(values) {
  // Tiny inline SVG sparkline. values is an array of small ints.
  if (!values || values.length === 0) {
    return '<svg class="tg-spark" viewBox="0 0 80 18"></svg>';
  }
  const max = Math.max(1, ...values);
  const w = 80, h = 18;
  const step = w / Math.max(1, values.length - 1);
  const points = values.map((v, i) => {
    const x = i * step;
    const y = h - (v / max) * (h - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<svg class="tg-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="1.2"/>
  </svg>`;
}

function tgFlags(item) {
  const flags = [];
  const cf = item.candidate_facts || {};
  const businesses = cf.businesses || [];
  for (const b of businesses) {
    const lower = (b || '').toLowerCase();
    if (lower === 'redhot') flags.push({ class: 'redhot', label: 'RedHot' });
    else if (lower === 'borderline') flags.push({ class: 'borderline', label: 'Borderline' });
    else if (lower) flags.push({ class: '', label: escapeHtml(b) });
  }
  // Due chip
  if (cf.due_at) {
    try {
      const dueDate = new Date(cf.due_at);
      const now = new Date();
      const diff = (dueDate - now) / (24 * 60 * 60 * 1000);
      const overdue = diff < 0.5;
      const lbl = fmtDate(cf.due_at).split(' ')[0] || 'due';
      flags.push({ class: 'due' + (overdue ? ' over' : ''), label: `due ${lbl}` });
    } catch (_e) { /* skip */ }
  }
  // Validation conflict
  if (item.validation_errors && item.validation_errors.length > 0) {
    flags.push({ class: 'conflict', label: 'conflict' });
  }
  // Approved status
  if (item.status === 'approved') {
    flags.push({ class: 'approved', label: 'approved' });
  }
  // Snoozed (only renders when item is currently snoozed; the inbox
  // view filters these out so the chip only ever shows in the
  // Snoozed sub-tab — informational, not redundant).
  if (tgIsSnoozed(item)) {
    let when = '';
    try {
      const d = new Date(item.snoozed_until);
      const todayMidnight = new Date(); todayMidnight.setHours(0, 0, 0, 0);
      const tomMidnight = new Date(todayMidnight); tomMidnight.setDate(tomMidnight.getDate() + 1);
      const dayAfterTom = new Date(todayMidnight); dayAfterTom.setDate(dayAfterTom.getDate() + 2);
      if (d < tomMidnight) when = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
      else if (d < dayAfterTom) when = 'tomorrow';
      else when = d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    } catch (_e) {}
    flags.push({ class: 'snoozed', label: `snoozed ${when}` });
  }
  return flags;
}

function tgSourceLabel(item) {
  const cf = item.candidate_facts || {};
  const ev = cf.source_event || cf.source_metadata || {};
  if (ev.channel_name) return `#${ev.channel_name}`;
  if (ev.channel_id) return ev.channel_id;
  if (item.source) return item.source.replace(/_/g, ' ');
  return '—';
}

function tgRenderRow(item, index) {
  const focused = state.review.focusedIndex === index;
  const selected = state.review.selectedIds.has(item.id);
  const ageMin = tgAgeMinutes(item);
  const ageDays = ageMin / (60 * 24);
  const dotClass = ageDays >= TG.vstaleDays ? 'vstale-dot'
    : ageDays >= TG.staleDays ? 'stale-dot' : '';
  const actClass = tgActionClass(item.proposed_action);
  const actLabel = tgActionLabel(item.proposed_action);
  const conf = item.confidence != null ? Number(item.confidence) : null;
  const confTier = tgConfTier(conf);
  const confStr = conf != null ? conf.toFixed(2) : '—';
  const confPct = conf != null ? Math.max(0, Math.min(100, conf * 100)) : 0;
  const cf = item.candidate_facts || {};
  const summary = cf.summary || '—';
  const source = tgSourceLabel(item);
  const flags = tgFlags(item);
  const stateClasses = (item.status === 'approved' ? ' approved' : '')
    + (item.status === 'snoozed' ? ' suppressed' : '');
  return `
    <div class="tg-row ${focused ? 'focused' : ''} ${selected ? 'selected' : ''}${stateClasses}"
         data-tg-index="${index}" data-tg-id="${escapeHtml(item.id)}">
      <div class="tg-age">
        <span class="tg-checkmark" data-tg-checkbox></span>
        ${dotClass ? `<span class="${dotClass}"></span>` : ''}
        ${escapeHtml(tgAgeStr(ageMin))}
      </div>
      <div class="tg-action ${actClass}">${escapeHtml(actLabel)}</div>
      <div class="tg-conf ${confTier}">
        <span>${escapeHtml(confStr)}</span>
        <div class="tg-confbar"><i style="width:${confPct.toFixed(0)}%"></i></div>
      </div>
      <div class="tg-source"><span>${escapeHtml(source)}</span></div>
      <div class="tg-summary">${escapeHtml(summary)}</div>
      <div class="tg-flags">
        ${flags.map(f => `<span class="tg-chip ${f.class}">${f.label}</span>`).join('')}
      </div>
    </div>
  `;
}

function tgRenderForecast() {
  const fc = tgForecastCounts();
  const cards = [
    { key: 'fresh',  label: 'Fresh today',         num: fc.fresh,  warn: false, danger: false },
    { key: 'warm',   label: 'Pending 1-3d',        num: fc.warm,   warn: false, danger: false },
    { key: 'stale',  label: 'Stale 3d+',           num: fc.stale,  warn: fc.stale > 0,  danger: false },
    { key: 'vstale', label: 'Stale 7d+',           num: fc.vstale, warn: false, danger: fc.vstale > 0 },
    { key: 'urgent', label: 'Urgent / ambiguous',  num: fc.urgent, warn: false, danger: fc.urgent > 0 },
  ];
  // Fake spark history (we don't persist a time series yet) — flat
  // line for now; populated from real metrics in Phase UI-8.
  const spark = tgRenderSpark([2, 3, 2, 4, 3, 5, fc.fresh + fc.warm]);
  return `
    <div class="tg-forecast" role="tablist" aria-label="Forecast">
      ${cards.map(c => {
        const numClass = c.danger ? 'danger' : (c.warn ? 'warn' : '');
        const active = state.review.forecastFilter === c.key ? 'active' : '';
        return `
          <div class="tg-fc ${active}" data-tg-forecast="${c.key}">
            <div class="tg-fc-lbl">${escapeHtml(c.label)}</div>
            <div class="tg-fc-num ${numClass}">${c.num}</div>
            ${spark}
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function tgRenderSubtabs(counts) {
  const tabs = [
    { key: 'inbox',     label: 'Inbox',           count: counts.inbox },
    { key: 'stale',     label: 'Stale',           count: counts.stale },
    { key: 'snoozed',   label: 'Snoozed',         count: counts.snoozed },
    { key: 'completed', label: 'Completed today', count: counts.completed },
  ];
  const cur = state.review.subview || 'inbox';
  return `
    <div class="tg-subtabs">
      ${tabs.map(t => `
        <button class="tg-subtab ${cur === t.key ? 'active' : ''}" data-tg-subtab="${t.key}">
          ${escapeHtml(t.label)} <span class="ct">${t.count}</span>
        </button>
      `).join('')}
      <div class="grow"></div>
      <div class="tg-search">
        <span style="font-size:12px">/</span>
        <input type="text" placeholder="Filter..." id="tg-search-input"
               value="${escapeHtml(state.review.searchQuery || '')}">
      </div>
    </div>
  `;
}

function tgEditForm(item) {
  // Render the inline edit form for a review_item. Action-specific
  // field set; fields are pre-populated from candidate_facts +
  // current proposed_patch. Save triggers PATCH /v1/review/{id}.
  const d = state.review.editDraft || {};
  const action = item.proposed_action;
  const cf = item.candidate_facts || {};
  const errBlock = state.review.editError
    ? `<div class="tg-qa-error">${escapeHtml(state.review.editError)}</div>`
    : '';
  // Convert ISO -> datetime-local string for the input.
  function isoToLocal(iso) {
    if (!iso) return '';
    try {
      const dt = new Date(iso);
      if (isNaN(dt.getTime())) return '';
      const pad = n => String(n).padStart(2, '0');
      return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`
           + `T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
    } catch (_e) { return ''; }
  }
  // Driven by action:
  // CREATE_TASK -> summary, due_at, category, dependency_text
  // UPDATE_TASK -> same patch fields (any subset)
  // COMPLETE_TASK -> completion_note
  let fields = '';
  if (action === 'CREATE_TASK' || action === 'UPDATE_TASK') {
    fields = `
      <div>
        <div class="tg-field-lbl">Summary</div>
        <input type="text" id="tg-edit-summary" maxlength="4096"
               value="${escapeHtml(d.summary != null ? d.summary : (cf.summary || ''))}">
      </div>
      <div class="tg-qa-row">
        <div class="tg-qa-field">
          <div class="tg-field-lbl">Due / when</div>
          <input type="datetime-local" id="tg-edit-due"
                 value="${escapeHtml(d.due_at_local != null ? d.due_at_local : isoToLocal(cf.due_at))}">
        </div>
        <div class="tg-qa-field">
          <div class="tg-field-lbl">Category</div>
          <input type="text" id="tg-edit-category" maxlength="64"
                 value="${escapeHtml(d.category != null ? d.category : (cf.category || ''))}">
        </div>
      </div>
      <div>
        <div class="tg-field-lbl">Dependency / blocked on (optional)</div>
        <input type="text" id="tg-edit-dependency" maxlength="2048"
               value="${escapeHtml(d.dependency_text != null ? d.dependency_text : (cf.dependency_text || ''))}">
      </div>
    `;
  } else if (action === 'COMPLETE_TASK') {
    fields = `
      <div>
        <div class="tg-field-lbl">Completion note (optional)</div>
        <textarea id="tg-edit-completion-note" maxlength="4096">${escapeHtml(d.completion_note != null ? d.completion_note : (cf.completion_note || ''))}</textarea>
      </div>
    `;
  } else {
    fields = `<div class="tg-qa-error">Edit not supported for action ${escapeHtml(action)}.</div>`;
  }
  const saveDisabled = state.review.editSaving ? 'disabled' : '';
  return `
    <div class="tg-edit-form">
      ${errBlock}
      ${fields}
      <div>
        <div class="tg-field-lbl">Edit reason (optional)</div>
        <input type="text" id="tg-edit-reason" maxlength="2048" placeholder="why are you editing this proposal?"
               value="${escapeHtml(d.edit_reason != null ? d.edit_reason : '')}">
      </div>
      <div class="tg-actions-row">
        <button class="tg-btn primary" data-tg-edit-save ${saveDisabled}>
          ${state.review.editSaving ? 'Saving…' : 'Save edits'}
        </button>
        <button class="tg-btn" data-tg-edit-cancel ${saveDisabled}>Cancel</button>
      </div>
    </div>
  `;
}

function tgRenderDetail(item) {
  if (!item) {
    return `<aside class="tg-detail"><div class="tg-detail-empty">Focus a row (J/K) to see source + facts here.</div></aside>`;
  }
  const isEditing = state.review.editingId === item.id;
  const ageMin = tgAgeMinutes(item);
  const cf = item.candidate_facts || {};
  const ev = cf.source_event || cf.source_metadata || {};
  const action = item.proposed_action || '—';
  const conf = item.confidence != null ? Number(item.confidence).toFixed(2) : '—';
  const businesses = (cf.businesses || []).join(', ');
  const owner = cf.owner_display || '<span class="missing">— unassigned —</span>';
  const due = cf.due_at ? fmtDate(cf.due_at) : '<span class="missing">— none —</span>';
  const priority = cf.priority || '<span class="missing">— normal —</span>';
  const category = cf.category || '<span class="missing">— uncategorized —</span>';
  const sourceQuote = cf.source_quote || ev.text || '';
  const verrors = item.validation_errors || [];
  const retrieved = item.retrieved_candidates || [];
  return `
    <aside class="tg-detail">
      <div class="tg-detail-h">
        <div class="tg-detail-meta">
          <span class="tg-action ${tgActionClass(action)}">${escapeHtml(tgActionLabel(action))}</span>
          <span class="sep">·</span>
          <span>conf ${escapeHtml(conf)}</span>
          <span class="sep">·</span>
          <span>${escapeHtml(tgAgeStr(ageMin))} old</span>
          ${item.status !== 'pending' ? `<span class="sep">·</span><span class="tg-chip ${item.status === 'approved' ? 'approved' : ''}">${escapeHtml(item.status)}</span>` : ''}
        </div>
        <h2 class="tg-detail-title">${escapeHtml(cf.summary || '—')}</h2>
      </div>
      <div class="tg-detail-body">
        <div>
          <div class="tg-sec-h">Source</div>
          <div class="tg-src-card">
            <div class="tg-src-head">
              <span class="who">${escapeHtml(ev.user_name || ev.user_id || item.source || 'unknown')}</span>
              <span>·</span>
              <span>${escapeHtml(tgSourceLabel(item))}</span>
            </div>
            ${sourceQuote ? `<div class="tg-src-quote">"${escapeHtml(sourceQuote)}"</div>` : '<div class="tg-src-quote" style="color:var(--tg-fg-dim)">(no source quote)</div>'}
          </div>
        </div>
        <div>
          <div class="tg-sec-h">${isEditing ? 'Edit proposal' : 'Proposed facts'}</div>
          ${isEditing ? tgEditForm(item) : `
            <dl class="tg-kvgrid">
              <dt>Summary</dt><dd>${escapeHtml(cf.summary || '—')}</dd>
              <dt>Business</dt><dd>${escapeHtml(businesses) || '<span class="missing">— missing —</span>'}</dd>
              <dt>Assignee</dt><dd>${owner}</dd>
              <dt>Priority</dt><dd>${priority}</dd>
              <dt>Due</dt><dd>${due}</dd>
              <dt>Category</dt><dd>${category}</dd>
            </dl>
          `}
        </div>
        ${retrieved.length ? `
          <div>
            <div class="tg-sec-h">Retrieved candidates (${retrieved.length})</div>
            ${retrieved.map(r => `
              <div class="tg-candrow">
                <span>${escapeHtml((r.summary || '').slice(0, 80))}</span>
                <span class="cand-conf">${r.lex_score != null ? `score ${r.lex_score}` : ''}</span>
              </div>
            `).join('')}
          </div>
        ` : `
          <div>
            <div class="tg-sec-h">Retrieved candidates</div>
            <div style="color:var(--tg-fg-dim);font-size:12.5px">No prior tasks matched — will create new.</div>
          </div>
        `}
        <div>
          <div class="tg-sec-h">Validation</div>
          ${verrors.length === 0
            ? '<div style="color:var(--tg-c-create);font-size:12.5px">✓ No issues.</div>'
            : `<div>${verrors.map(e => `<div style="color:var(--tg-c-amb);font-size:12.5px">⚠ <code>${escapeHtml(e.code || '')}</code> ${escapeHtml(e.message || '')}</div>`).join('')}</div>`
          }
        </div>
      </div>
      ${(item.status === 'pending' || item.status === 'needs_changes') && !isEditing ? `
        <div class="tg-actions-row">
          <button class="tg-btn primary" data-tg-detail-approve>
            Approve <span class="tg-pill-kbd">1</span>
          </button>
          <button class="tg-btn" data-tg-detail-edit title="Edit proposal then approve">
            Edit <span class="tg-pill-kbd">3</span>
          </button>
          <button class="tg-btn danger" data-tg-detail-reject>
            Reject <span class="tg-pill-kbd">2</span>
          </button>
          ${tgIsSnoozed(item)
            ? `<button class="tg-btn" data-tg-detail-unsnooze title="Clear snooze">Un-snooze</button>`
            : `<button class="tg-btn" data-tg-detail-snooze title="Snooze">
                 Snooze <span class="tg-pill-kbd">H</span>
               </button>`
          }
        </div>
      ` : ''}
    </aside>
  `;
}

function tgRenderBulkBar() {
  const ids = Array.from(state.review.selectedIds);
  if (ids.length === 0) return '<div class="tg-bulkbar"></div>';
  // Group by action type for the breakdown
  const byAction = {};
  for (const id of ids) {
    const item = state.review.items.find(i => i.id === id);
    if (!item) continue;
    const a = tgActionLabel(item.proposed_action);
    byAction[a] = (byAction[a] || 0) + 1;
  }
  const breakdown = Object.entries(byAction)
    .map(([k, v]) => `${k} ${v}`).join(' · ');
  return `
    <div class="tg-bulkbar show">
      <div class="tg-bb-count"><span class="acc">${ids.length}</span> selected</div>
      <div class="tg-bb-grp">${escapeHtml(breakdown)}</div>
      <button class="tg-btn primary" data-tg-bulk-approve ${state.review.bulkInProgress ? 'disabled' : ''}>
        Approve <span class="tg-pill-kbd">1</span>
      </button>
      <button class="tg-btn danger" data-tg-bulk-reject ${state.review.bulkInProgress ? 'disabled' : ''}>
        Reject <span class="tg-pill-kbd">2</span>
      </button>
      <button class="tg-btn" data-tg-bulk-snooze title="Snooze selected items">
        Snooze <span class="tg-pill-kbd">H</span>
      </button>
      <button class="tg-btn" data-tg-bulk-clear>Clear <span class="tg-pill-kbd">Esc</span></button>
    </div>
  `;
}

function tgRenderRejectModal() {
  const m = state.review.rejectModal;
  if (!m) return '';
  const ids = m.ids || [];
  const items = ids.map(id => state.review.items.find(i => i.id === id)).filter(Boolean);
  const byAction = {};
  for (const it of items) {
    const a = tgActionLabel(it.proposed_action);
    byAction[a] = (byAction[a] || 0) + 1;
  }
  const breakdown = Object.entries(byAction).map(([k, v]) => `${k} ${v}`).join(' · ');
  return `
    <div class="tg-modal-wrap" data-tg-modal-backdrop>
      <div class="tg-modal" role="dialog" aria-label="Reject">
        <div class="tg-modal-h">
          Reject ${ids.length} item${ids.length === 1 ? '' : 's'}
          <span class="small">${escapeHtml(breakdown)}</span>
        </div>
        <div class="tg-modal-b">
          <div>
            <div class="tg-field-lbl">Quick reason (optional)</div>
            <div class="tg-reasons">
              ${TG.rejectPresets.map(p => `
                <button class="tg-reason ${m.preset === p ? 'on' : ''}" data-tg-reason="${escapeHtml(p)}">${escapeHtml(p)}</button>
              `).join('')}
            </div>
          </div>
          <div>
            <div class="tg-field-lbl">Or type a shared reason</div>
            <textarea id="tg-reject-textarea" placeholder="Why is this not a task?">${escapeHtml(m.reasonText || '')}</textarea>
          </div>
        </div>
        <div class="tg-modal-f">
          <button class="tg-btn" data-tg-modal-cancel>Cancel</button>
          <button class="tg-btn danger" data-tg-modal-submit>Reject ${ids.length}</button>
        </div>
      </div>
    </div>
  `;
}

function tgRenderToast() {
  if (!state.review.toast) return '';
  const t = state.review.toast;
  const cls = t.kind === 'err' ? 'err' : 'ok';
  return `<div class="tg-toast"><span class="${cls}">●</span> ${escapeHtml(t.msg)}</div>`;
}

// ---------------------------------------------------------------------------
// Phase UI-2B1: Snooze modal.
// ---------------------------------------------------------------------------

// Compute the four quick-snooze tile presets, anchored at "now" in
// the local timezone. Returns label + ISO UTC timestamp.
function tgSnoozePresets() {
  const now = new Date();
  function nextAt(hour, minute, dayOffset) {
    const d = new Date(now);
    d.setDate(d.getDate() + dayOffset);
    d.setHours(hour, minute, 0, 0);
    return d;
  }
  // 1. Plus 1 hour
  const oneHour = new Date(now.getTime() + 60 * 60 * 1000);
  // 2. Tomorrow 9am (local)
  const tomorrow9 = nextAt(9, 0, 1);
  // 3. Next Monday 9am (local). 0 = Sun.
  const dow = now.getDay();
  // days until next Monday: if today is Mon, jump to NEXT Mon (7).
  const daysToMon = ((1 - dow + 7) % 7) || 7;
  const nextMon = nextAt(9, 0, daysToMon);
  return [
    { key: '1h',     label: '+1 hour',       sub: oneHour.toLocaleString(undefined, { hour: 'numeric', minute: '2-digit' }), iso: oneHour.toISOString() },
    { key: 'tom9',   label: 'Tomorrow 9am',  sub: tomorrow9.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }), iso: tomorrow9.toISOString() },
    { key: 'mon9',   label: 'Mon 9am',       sub: nextMon.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }), iso: nextMon.toISOString() },
    { key: 'custom', label: 'Pick a date…',  sub: 'datetime', iso: null },
  ];
}

function tgOpenSnoozeModal(ids) {
  state.review.snoozeModal = {
    ids: Array.isArray(ids) ? ids.slice() : [ids],
    selectedKey: 'tom9',     // sensible default
    customIso: '',
    reasonText: '',
  };
  render();
}

function tgRenderSnoozeModal() {
  const m = state.review.snoozeModal;
  if (!m) return '';
  const ids = m.ids || [];
  const presets = tgSnoozePresets();
  const tiles = presets.map(p => `
    <div class="qd ${m.selectedKey === p.key ? 'on' : ''}" data-tg-snooze-tile="${p.key}">
      ${escapeHtml(p.label)}
      <small>${escapeHtml(p.sub)}</small>
    </div>
  `).join('');
  // Show the custom-date input only when "Pick a date…" is selected.
  const customBlock = m.selectedKey === 'custom' ? `
    <div>
      <div class="tg-field-lbl">Custom date and time (your local timezone)</div>
      <input type="datetime-local" id="tg-snooze-custom" value="${escapeHtml(m.customIso || '')}">
    </div>
  ` : '';
  return `
    <div class="tg-modal-wrap" data-tg-modal-backdrop>
      <div class="tg-modal" role="dialog" aria-label="Snooze">
        <div class="tg-modal-h">
          Snooze ${ids.length} item${ids.length === 1 ? '' : 's'}
          <span class="small">re-surfaces automatically</span>
        </div>
        <div class="tg-modal-b">
          <div>
            <div class="tg-field-lbl">Snooze until</div>
            <div class="quickdates">${tiles}</div>
          </div>
          ${customBlock}
          <div>
            <div class="tg-field-lbl">Note (optional)</div>
            <textarea id="tg-snooze-textarea" placeholder="Why are you setting this aside?">${escapeHtml(m.reasonText || '')}</textarea>
          </div>
        </div>
        <div class="tg-modal-f">
          <button class="tg-btn" data-tg-snooze-cancel>Cancel</button>
          <button class="tg-btn primary" data-tg-snooze-submit>Snooze ${ids.length}</button>
        </div>
      </div>
    </div>
  `;
}

// Resolve the modal selection into a concrete ISO string, or null if
// the operator picked Custom but didn't fill in a value.
function tgResolveSnoozeIso(m) {
  if (!m) return null;
  if (m.selectedKey === 'custom') {
    const raw = (m.customIso || '').trim();
    if (!raw) return null;
    // datetime-local gives "2026-05-09T18:00" with no timezone. Treat
    // as local time and convert to ISO UTC for the API.
    const d = new Date(raw);
    if (Number.isNaN(d.getTime())) return null;
    return d.toISOString();
  }
  const presets = tgSnoozePresets();
  const p = presets.find(x => x.key === m.selectedKey);
  return p ? p.iso : null;
}

async function _tgApplySnooze() {
  const m = state.review.snoozeModal;
  if (!m) return;
  const iso = tgResolveSnoozeIso(m);
  if (!iso) {
    tgShowToast('Pick a snooze deadline', 'err');
    return;
  }
  const ids = m.ids || [];
  if (ids.length === 0) { state.review.snoozeModal = null; render(); return; }
  let okCount = 0;
  const errors = [];
  for (const id of ids) {
    try {
      await api(`/v1/review/${encodeURIComponent(id)}/snooze`, {
        method: 'POST',
        body: { snoozed_until: iso, reason: m.reasonText || null },
      });
      okCount += 1;
    } catch (e) {
      errors.push({ id, err: e && e.message || String(e) });
    }
  }
  // Clear modal + selection, refetch list so the snoozed rows
  // disappear from Inbox.
  state.review.snoozeModal = null;
  state.review.selectedIds = new Set();
  try {
    const result = await loadReviewItems(state.review.statusFilter);
    state.review.items = result.items;
    state.review.total = result.total;
  } catch (_e) { /* leave stale list */ }
  render();
  if (errors.length === 0) {
    tgShowToast(`Snoozed ${okCount}`, 'ok');
  } else {
    tgShowToast(`Snoozed ${okCount}/${ids.length}; ${errors.length} failed`, 'err');
  }
}

// ---------------------------------------------------------------------------
// Phase UI-2B4: Edit-then-approve.
// Inline form on the focused review_item that PATCHes proposed_patch
// then leaves the item in edit-then-pending so the operator can hit
// 1 to approve or step away.
// ---------------------------------------------------------------------------

function _tgEnterEditMode(item) {
  const cf = item.candidate_facts || {};
  // Convert ISO due_at -> datetime-local string for the input.
  let dueLocal = '';
  if (cf.due_at) {
    try {
      const dt = new Date(cf.due_at);
      if (!isNaN(dt.getTime())) {
        const pad = n => String(n).padStart(2, '0');
        dueLocal = `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`
                 + `T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
      }
    } catch (_e) {}
  }
  state.review.editingId = item.id;
  state.review.editDraft = {
    action: item.proposed_action,
    summary: cf.summary || '',
    due_at_local: dueLocal,
    category: cf.category || '',
    dependency_text: cf.dependency_text || '',
    completion_note: cf.completion_note || '',
    edit_reason: '',
  };
  state.review.editError = null;
  state.review.editSaving = false;
  render();
  requestAnimationFrame(() => {
    const sumEl = document.getElementById('tg-edit-summary');
    if (sumEl) {
      sumEl.focus();
      try { sumEl.setSelectionRange(sumEl.value.length, sumEl.value.length); } catch (_e) {}
    }
  });
}

function _tgSnapshotEditForm() {
  const sumEl = document.getElementById('tg-edit-summary');
  const dueEl = document.getElementById('tg-edit-due');
  const catEl = document.getElementById('tg-edit-category');
  const depEl = document.getElementById('tg-edit-dependency');
  const noteEl = document.getElementById('tg-edit-completion-note');
  const reasonEl = document.getElementById('tg-edit-reason');
  const d = state.review.editDraft || {};
  if (sumEl) d.summary = sumEl.value;
  if (dueEl) d.due_at_local = dueEl.value;
  if (catEl) d.category = catEl.value;
  if (depEl) d.dependency_text = depEl.value;
  if (noteEl) d.completion_note = noteEl.value;
  if (reasonEl) d.edit_reason = reasonEl.value;
  state.review.editDraft = d;
  return d;
}

async function _tgSaveEdit() {
  const id = state.review.editingId;
  if (!id) return;
  const d = _tgSnapshotEditForm();
  // datetime-local local time -> UTC ISO.
  let dueIso = null;
  if (d.due_at_local) {
    const dt = new Date(d.due_at_local);
    if (!isNaN(dt.getTime())) dueIso = dt.toISOString();
  }
  // Build the typed PATCH body. The endpoint requires exactly one of
  // create/update/complete and the key MUST match proposed_action.
  let body = { edit_reason: (d.edit_reason || '').trim() || null };
  if (d.action === 'CREATE_TASK') {
    if (!d.summary || !d.summary.trim()) {
      state.review.editError = 'Summary is required';
      render();
      return;
    }
    // Pull current businesses from the item (PATCH endpoint requires
    // them in the body for CREATE; we don't change them in this UI).
    const focused = state.review.items_visible.find(i => i.id === id);
    const cf = (focused && focused.candidate_facts) || {};
    const businesses = cf.businesses || [];
    if (!businesses.length) {
      state.review.editError = 'No business slug on the item — cannot save';
      render();
      return;
    }
    body.create = {
      summary: d.summary.trim(),
      due_at: dueIso,
      category: (d.category || '').trim() || null,
      dependency_text: (d.dependency_text || '').trim() || null,
      businesses,
    };
  } else if (d.action === 'UPDATE_TASK') {
    body.update = {};
    if (d.summary && d.summary.trim()) body.update.summary = d.summary.trim();
    if (dueIso) body.update.due_at = dueIso;
    if ((d.category || '').trim()) body.update.category = d.category.trim();
    if ((d.dependency_text || '').trim()) body.update.dependency_text = d.dependency_text.trim();
    if (Object.keys(body.update).length === 0) {
      state.review.editError = 'No fields changed';
      render();
      return;
    }
  } else if (d.action === 'COMPLETE_TASK') {
    body.complete = {
      completion_note: (d.completion_note || '').trim() || null,
    };
  } else {
    state.review.editError = `Edit not supported for ${d.action}`;
    render();
    return;
  }
  state.review.editSaving = true;
  state.review.editError = null;
  render();
  try {
    await api(`/v1/review/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body,
    });
  } catch (e) {
    state.review.editSaving = false;
    state.review.editError = (e && e.message) || 'Save failed';
    render();
    return;
  }
  // Re-fetch the queue so the edited item shows updated facts, then
  // exit edit mode. Operator can now press 1 to approve.
  const editedId = id;
  state.review.editSaving = false;
  state.review.editingId = null;
  state.review.editDraft = null;
  state.review.editError = null;
  try {
    const result = await loadReviewItems(state.review.statusFilter);
    state.review.items = result.items;
    state.review.total = result.total;
  } catch (_e) {}
  // B4 blocker: re-focus by id, not by stale index. Refetch may
  // have shifted the queue; "press 1 to approve" must hit the
  // edited row, not whatever now sits at the previous index.
  // tgSubviewItems() rebuilds from the current state, so we have
  // to mirror the visible-list filter logic.
  render();
  // The render() call has set state.review.items_visible. Find the
  // edited item's NEW index there.
  const visible = state.review.items_visible || [];
  const newIdx = visible.findIndex(i => i.id === editedId);
  if (newIdx >= 0) {
    state.review.focusedIndex = newIdx;
    render();
    tgShowToast('Edits saved — press 1 to approve', 'ok');
  } else {
    // Edited item dropped out of the current visible set under
    // the active sub-tab + forecast + search filter. Clear focus
    // AND any stale multi-select so the global '1' shortcut
    // can't approve whatever row(s) the operator picked before
    // editing (Codex round-3 blocker — _ui1ActionTargets prefers
    // selectedIds over focusedIndex when both are present).
    state.review.focusedIndex = null;
    state.review.selectedIds = new Set();
    render();
    tgShowToast('Edits saved — clear filters to find the row', 'ok');
  }
}

async function _tgUnsnooze(id) {
  try {
    await api(`/v1/review/${encodeURIComponent(id)}/unsnooze`, { method: 'POST', body: {} });
  } catch (e) {
    tgShowToast(`Un-snooze failed: ${e && e.message || e}`, 'err');
    return;
  }
  try {
    const result = await loadReviewItems(state.review.statusFilter);
    state.review.items = result.items;
    state.review.total = result.total;
  } catch (_e) {}
  render();
  tgShowToast('Un-snoozed', 'ok');
}

// ---------------------------------------------------------------------------
// Phase UI-2B2: Quick Add compose modal.
// Slack-style ad-hoc task / event capture from any view.
// ---------------------------------------------------------------------------

let _tgQuickAddHostKey = null;

function _tgQuickAddStructuralKey() {
  const q = state.quickAdd;
  if (!q || !q.open) return 'closed';
  const dedup = q.dedup
    ? (q.dedup.candidates || []).map(c => [
        c.id || '',
        c.summary || '',
        c.status || '',
        c.due_at || '',
        c.last_activity_at || '',
      ].join('|')).join('~')
    : '';
  return [
    'open',
    q.submitting ? 'submitting' : 'idle',
    q.error || '',
    q.dedup ? 'dedup' : 'form',
    q.dedup && q.dedup.previewToken ? q.dedup.previewToken : '',
    dedup,
  ].join('\u001f');
}

function _tgEnsureQuickAddHost() {
  let host = document.getElementById('tg-quickadd-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'tg-quickadd-host';
    document.body.appendChild(host);
  }
  return host;
}

function _tgInvalidateQuickAddHost() {
  _tgQuickAddHostKey = null;
}

function _tgRenderQuickAddHost() {
  const host = _tgEnsureQuickAddHost();
  const key = _tgQuickAddStructuralKey();
  if (key === _tgQuickAddHostKey) return;
  host.innerHTML = (state.quickAdd && state.quickAdd.open)
    ? tgRenderQuickAddModal()
    : '';
  _tgQuickAddHostKey = key;
}

function tgOpenQuickAdd() {
  const p = state.principal || {};
  // Default the business to the principal's first visible business.
  // Admins (visibility = all) get the global businesses[] list's
  // first slug. Reset other fields each time the modal opens.
  let defaultSlug = '';
  if (p.businesses && p.businesses.length > 0) {
    defaultSlug = p.businesses[0].slug || '';
  }
  if (!defaultSlug && state.businesses && state.businesses.length > 0) {
    defaultSlug = state.businesses[0].slug || '';
  }
  state.quickAdd = {
    open: true,
    summary: '',
    businessSlug: defaultSlug,
    dueAtLocal: '',
    description: '',
    kind: 'task',
    submitting: false,
    error: null,
    ownerUserId: '',
    members: [],
    membersLoading: false,
    dedup: null,
  };
  _tgInvalidateQuickAddHost();
  render();
  // Focus the title input on next tick so the operator can start
  // typing immediately. requestAnimationFrame is safer than setTimeout
  // because it fires after the render commits to the DOM.
  requestAnimationFrame(() => {
    const el = document.getElementById('tg-qa-summary');
    if (el) el.focus();
  });
  // Kick off member load for the default business so the assignee
  // dropdown is populated by the time the operator tabs to it.
  if (defaultSlug) _tgLoadMembers(defaultSlug);
}

// Surgical update of the assignee dropdown WITHOUT re-rendering
// the whole modal. Codex/Kyle B3-2-fix blocker: a full render()
// destroys the active <input> element, dropping focus and (on
// mobile) dismissing the soft keyboard while the operator is mid-
// type. By rebuilding only #tg-qa-owner and the hint span we keep
// the summary/notes/due inputs alive across the async member load.
function _tgUpdateOwnerDropdown() {
  const q = state.quickAdd;
  if (!q || !q.open) return;
  const sel = document.getElementById('tg-qa-owner');
  if (!sel) return;
  const p = state.principal || {};
  const principalId = p.id;
  const meRow = q.members.find(m => m.id === principalId);
  const others = q.members.filter(m => m.id !== principalId);
  const meLabel = meRow ? `${meRow.display_name} (me)` : 'Me';
  sel.innerHTML = `
    <option value="" ${q.ownerUserId === '' ? 'selected' : ''}>— Unassigned —</option>
    ${meRow ? `<option value="${escapeHtml(principalId)}" ${q.ownerUserId === principalId ? 'selected' : ''}>${escapeHtml(meLabel)}</option>` : ''}
    ${others.map(m => `
      <option value="${escapeHtml(m.id)}" ${q.ownerUserId === m.id ? 'selected' : ''}>
        ${escapeHtml(m.display_name)}
      </option>
    `).join('')}
  `;
  // Hint span next to the field label.
  const hintSpan = document.querySelector('.tg-qa-hint');
  if (hintSpan) {
    if (q.membersLoading) {
      hintSpan.textContent = 'loading members…';
      hintSpan.style.display = '';
    } else if (q.members.length === 0 && q.businessSlug) {
      hintSpan.textContent = 'no members in this business yet';
      hintSpan.style.display = '';
    } else {
      hintSpan.textContent = '';
      hintSpan.style.display = 'none';
    }
  }
}

async function _tgLoadMembers(slug) {
  if (!slug) {
    state.quickAdd.members = [];
    _tgUpdateOwnerDropdown();
    return;
  }
  state.quickAdd.membersLoading = true;
  state.quickAdd.members = [];
  _tgUpdateOwnerDropdown();   // surgical update; preserves focus.
  try {
    const data = await api(`/v1/businesses/${encodeURIComponent(slug)}/members`);
    // Only apply if the modal is still on the same business — the
    // operator may have switched while the request was in flight.
    if (state.quickAdd.businessSlug === slug) {
      state.quickAdd.members = data.members || [];
      // Default ownerUserId = creator if creator is a member.
      const principalId = state.principal && state.principal.id;
      if (!state.quickAdd.ownerUserId
          && state.quickAdd.members.some(m => m.id === principalId)) {
        state.quickAdd.ownerUserId = principalId;
      }
    }
  } catch (e) {
    if (state.quickAdd.businessSlug === slug) {
      console.warn('failed to load business members:', e);
    }
  } finally {
    if (state.quickAdd.businessSlug === slug) {
      state.quickAdd.membersLoading = false;
      _tgUpdateOwnerDropdown();   // surgical update; preserves focus.
    }
  }
}

// Compute datetime-local string for "today at hour:minute" / "+N days
// at hour:minute" anchored to the operator's local timezone.
function _tgDueChipPresets() {
  const now = new Date();
  function localISO(d) {
    // datetime-local wants "YYYY-MM-DDTHH:MM" in LOCAL time, no Z.
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
         + `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function nextAt(hour, minute, dayOffset) {
    const d = new Date(now);
    d.setDate(d.getDate() + dayOffset);
    d.setHours(hour, minute, 0, 0);
    return d;
  }
  // Today 5pm — but if it's already past 5pm, hop to tomorrow 9am.
  const today5 = nextAt(17, 0, 0);
  const todayLabel = today5 > now ? 'Today 5pm' : 'Tomorrow 9am';
  const todayValue = today5 > now ? today5 : nextAt(9, 0, 1);
  // Next Mon 9am — if today IS Monday, jump 7 days.
  const dow = now.getDay();
  const daysToMon = ((1 - dow + 7) % 7) || 7;
  const nextMon = nextAt(9, 0, daysToMon);
  // +1 hour from now, rounded to the next 5-min mark for tidiness.
  const oneHour = new Date(now.getTime() + 60 * 60 * 1000);
  oneHour.setMinutes(Math.ceil(oneHour.getMinutes() / 5) * 5, 0, 0);
  return [
    { key: '1h',   label: '+1 hour',     value: localISO(oneHour) },
    { key: 'tod',  label: todayLabel,    value: localISO(todayValue) },
    { key: 'tom',  label: 'Tomorrow 9am', value: localISO(nextAt(9, 0, 1)) },
    { key: 'mon',  label: 'Mon 9am',     value: localISO(nextMon) },
    { key: 'none', label: 'No due',      value: '' },
  ];
}

function tgRenderQuickAddModal() {
  const q = state.quickAdd;
  if (!q || !q.open) return '';
  const p = state.principal || {};
  // Build business options: prefer the principal's visible
  // businesses (membership-scoped). Admins see them all.
  const sourceBiz = (p.businesses && p.businesses.length > 0)
    ? p.businesses
    : (state.businesses || []);
  const bizOptions = sourceBiz.map(b => `
    <option value="${escapeHtml(b.slug)}" ${q.businessSlug === b.slug ? 'selected' : ''}>
      ${escapeHtml(b.name || b.slug)}
    </option>
  `).join('');
  // Assignee options: principal's display name as "Me" first, then
  // other business members. Empty string == unassigned.
  const principalId = p.id;
  const meRow = q.members.find(m => m.id === principalId);
  const others = q.members.filter(m => m.id !== principalId);
  const meLabel = meRow ? `${meRow.display_name} (me)` : 'Me';
  const ownerOptions = `
    <option value="" ${q.ownerUserId === '' ? 'selected' : ''}>— Unassigned —</option>
    ${meRow ? `<option value="${escapeHtml(principalId)}" ${q.ownerUserId === principalId ? 'selected' : ''}>${escapeHtml(meLabel)}</option>` : ''}
    ${others.map(m => `
      <option value="${escapeHtml(m.id)}" ${q.ownerUserId === m.id ? 'selected' : ''}>
        ${escapeHtml(m.display_name)}
      </option>
    `).join('')}
  `;
  const ownerHint = q.membersLoading
    ? '<span class="tg-qa-hint">loading members…</span>'
    : (q.members.length === 0 && q.businessSlug
       ? '<span class="tg-qa-hint">no members in this business yet</span>'
       : '');
  const errorBlock = q.error
    ? `<div class="tg-qa-error">${escapeHtml(q.error)}</div>`
    : '';
  const submitDisabled = q.submitting ? 'disabled' : '';
  // The primary modal is hidden behind the dedup confirm (when
  // present) so the dedup decision feels modal-on-modal — operator
  // resolves dedup before they can edit the form again.
  // When dedup is showing, mark the underlying form `inert` so Tab
  // doesn't reach focusable elements behind the overlay (Codex B3-3
  // blocker).
  const dedupBlock = q.dedup ? tgRenderDedupConfirm(q.dedup) : '';
  const inertAttr = q.dedup ? 'inert aria-hidden="true"' : '';
  return `
    <div class="tg-modal-wrap" data-tg-qa-backdrop ${inertAttr}>
      <div class="tg-modal" role="dialog" aria-label="Quick Add">
        <div class="tg-modal-h">
          Quick add
          <span class="small">⌨ Q anywhere · Esc to close</span>
        </div>
        <div class="tg-modal-b">
          ${errorBlock}
          <div>
            <div class="tg-field-lbl">What needs doing?</div>
            <input type="text" id="tg-qa-summary"
                   placeholder="e.g. Call vendor about delayed shipment"
                   maxlength="4096"
                   value="${escapeHtml(q.summary || '')}">
          </div>
          <div class="tg-qa-row">
            <div class="tg-qa-field">
              <div class="tg-field-lbl">Kind</div>
              <select id="tg-qa-kind">
                <option value="task" ${q.kind === 'task' ? 'selected' : ''}>Task</option>
                <option value="event" ${q.kind === 'event' ? 'selected' : ''}>Event</option>
              </select>
            </div>
            <div class="tg-qa-field">
              <div class="tg-field-lbl">Business</div>
              <select id="tg-qa-business">${bizOptions}</select>
            </div>
            <div class="tg-qa-field">
              <div class="tg-field-lbl">Due / when</div>
              <input type="datetime-local" id="tg-qa-due"
                     value="${escapeHtml(q.dueAtLocal || '')}">
            </div>
          </div>
          <div class="tg-qa-due-chips">
            ${_tgDueChipPresets().map(p => `
              <button type="button" class="tg-qa-chip"
                      data-tg-qa-due-chip="${escapeHtml(p.value)}"
                      title="${escapeHtml(p.label)}">
                ${escapeHtml(p.label)}
              </button>
            `).join('')}
          </div>
          <div>
            <div class="tg-field-lbl">Assigned to ${ownerHint}</div>
            <select id="tg-qa-owner">${ownerOptions}</select>
          </div>
          <div>
            <div class="tg-field-lbl">Notes (optional)</div>
            <textarea id="tg-qa-description" maxlength="8192"
                      placeholder="Anything else useful here…">${escapeHtml(q.description || '')}</textarea>
          </div>
        </div>
        <div class="tg-modal-f">
          <button class="tg-btn" data-tg-qa-cancel ${submitDisabled}>Cancel</button>
          <button class="tg-btn primary" data-tg-qa-submit ${submitDisabled}>
            ${q.submitting ? 'Checking…' : 'Add'}
            <span class="tg-pill-kbd">↵</span>
          </button>
        </div>
      </div>
    </div>
    ${dedupBlock}
  `;
}

function tgRenderDedupConfirm(dedup) {
  // Secondary modal stacked over Quick Add. Operator sees up to 5
  // candidates that look like duplicates and decides whether to
  // navigate to one (Use existing), commit anyway (Add anyway), or
  // cancel back to the Quick Add form.
  //
  // UX (B3-3): each row is a flex grid with a clear summary, meta
  // line, and an explicit "Open" pill on the right so the click
  // affordance reads as obvious. Number keys 1-5 also pick rows.
  const candidates = dedup.candidates || [];
  const rows = candidates.map((c, i) => {
    const ts = c.last_activity_at
      ? fmtDate(c.last_activity_at).split(',')[0]
      : '';
    const dueChip = c.due_at
      ? `<span class="tg-dedup-due">due ${escapeHtml(fmtDate(c.due_at).split(',')[0])}</span>`
      : '';
    return `
      <button class="tg-dedup-row" data-tg-dedup-pick="${escapeHtml(c.id)}"
              aria-label="Open ${escapeHtml(c.summary || '')}">
        <span class="tg-dedup-num">${i + 1}</span>
        <span class="tg-dedup-body">
          <span class="tg-dedup-summary">${escapeHtml(c.summary || '')}</span>
          <span class="tg-dedup-meta">
            <span class="tg-dedup-status">${escapeHtml(c.status || '')}</span>
            ${dueChip}
            <span class="tg-dedup-activity">last activity ${escapeHtml(ts)}</span>
          </span>
        </span>
        <span class="tg-dedup-cta">
          Open
          <span class="tg-pill-kbd">${i + 1}</span>
        </span>
      </button>
    `;
  }).join('');
  return `
    <div class="tg-modal-wrap tg-dedup-wrap" data-tg-dedup-backdrop>
      <div class="tg-modal tg-dedup-modal" role="dialog" aria-label="Possible duplicate">
        <div class="tg-modal-h">
          Looks like an existing task
          <span class="small">${candidates.length} match${candidates.length === 1 ? '' : 'es'}</span>
        </div>
        <div class="tg-modal-b">
          <p class="tg-dedup-intro">Pick a row (or press its number) to open it, or add a new task anyway.</p>
          <div class="tg-dedup-list">${rows}</div>
        </div>
        <div class="tg-modal-f">
          <button class="tg-btn" data-tg-dedup-cancel>Back <span class="tg-pill-kbd">Esc</span></button>
          <button class="tg-btn primary" data-tg-dedup-force>Add anyway <span class="tg-pill-kbd">N</span></button>
        </div>
      </div>
    </div>
  `;
}

function _tgSnapshotQuickAddForm() {
  // Snapshot DOM values into state. Returns the snapshotted shape
  // (so callers don't have to re-read state right after).
  const q = state.quickAdd;
  const sumEl = document.getElementById('tg-qa-summary');
  const bizEl = document.getElementById('tg-qa-business');
  const dueEl = document.getElementById('tg-qa-due');
  const descEl = document.getElementById('tg-qa-description');
  const kindEl = document.getElementById('tg-qa-kind');
  const ownerEl = document.getElementById('tg-qa-owner');
  const summary = (sumEl ? sumEl.value : q.summary).trim();
  const businessSlug = (bizEl ? bizEl.value : q.businessSlug);
  const dueAtLocal = dueEl ? dueEl.value : q.dueAtLocal;
  const description = (descEl ? descEl.value : q.description).trim();
  const kind = kindEl ? kindEl.value : q.kind;
  const ownerUserId = ownerEl ? ownerEl.value : q.ownerUserId;
  state.quickAdd.summary = summary;
  state.quickAdd.businessSlug = businessSlug;
  state.quickAdd.dueAtLocal = dueAtLocal;
  state.quickAdd.description = description;
  state.quickAdd.kind = kind;
  state.quickAdd.ownerUserId = ownerUserId;
  return { summary, businessSlug, dueAtLocal, description, kind, ownerUserId };
}

async function _tgSubmitQuickAdd() {
  const q = state.quickAdd;
  if (!q || !q.open || q.submitting) return;
  const snap = _tgSnapshotQuickAddForm();
  if (!snap.summary) {
    state.quickAdd.error = 'Title is required';
    render();
    return;
  }
  if (!snap.businessSlug) {
    state.quickAdd.error = 'Pick a business';
    render();
    return;
  }
  let dueAtIso = null;
  if (snap.dueAtLocal) {
    const d = new Date(snap.dueAtLocal);
    if (!Number.isNaN(d.getTime())) dueAtIso = d.toISOString();
  }
  state.quickAdd.submitting = true;
  state.quickAdd.error = null;
  render();

  // ----- Step 1: preview (deterministic, no LLM, sub-second) -----
  let previewToken = null;
  try {
    const preview = await api('/v1/tasks/preview', {
      method: 'POST',
      body: {
        summary: snap.summary,
        business_slug: snap.businessSlug,
        due_at: dueAtIso,
      },
    });
    if (preview && Array.isArray(preview.candidates)
        && preview.candidates.length > 0) {
      // Surface the dedup modal. submitting=false so the form is
      // editable again after the operator backs out. The actual
      // create POST happens after they pick "Add anyway" or
      // "Use existing".
      state.quickAdd.dedup = {
        candidates: preview.candidates,
        previewToken: preview.preview_token,
      };
      state.quickAdd.submitting = false;
      render();
      // Focus the first dedup row so Tab/Shift-Tab cycles candidates
      // and number keys 1-5 fire from a body-focused element (the
      // typing-target bail does not eat them either way — handler is
      // above the bail — but explicit focus is the right UX).
      requestAnimationFrame(() => {
        const first = document.querySelector('.tg-dedup-row');
        if (first) first.focus();
      });
      return;
    }
    previewToken = preview && preview.preview_token;
  } catch (e) {
    // Preview failure is non-fatal — fall through to commit. The
    // server will still re-run retrieve at commit time if we send
    // the token, so dedup safety isn't compromised by a flaky
    // preview round-trip.
    console.warn('preview failed, proceeding to direct create:', e);
  }

  // ----- Step 2: commit -----
  await _tgCommitQuickAdd({
    snap, dueAtIso, previewToken, forceCreate: false,
  });
}

async function _tgCommitQuickAdd({ snap, dueAtIso, previewToken,
                                   forceCreate }) {
  state.quickAdd.submitting = true;
  state.quickAdd.error = null;
  render();
  let result;
  try {
    const idempotencyKey = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID()
      : `pwa-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    result = await api('/v1/tasks', {
      method: 'POST',
      body: {
        summary: snap.summary,
        business_slug: snap.businessSlug,
        due_at: dueAtIso,
        description: snap.description || null,
        kind: snap.kind,
        owner_user_id: snap.ownerUserId || null,
        idempotency_key: idempotencyKey,
        force_create: !!forceCreate,
        preview_token: previewToken || null,
      },
    });
  } catch (e) {
    state.quickAdd.submitting = false;
    // Server returned 409 duplicate_candidates_changed — re-show
    // dedup with the fresh list. The 'body' in api()'s thrown error
    // shape carries detail.candidates.
    if (e && e.status === 409 && e.body && e.body.detail
        && e.body.detail.code === 'duplicate_candidates_changed') {
      state.quickAdd.dedup = {
        candidates: e.body.detail.candidates || [],
        previewToken: null,  // force operator decision; no token replay
      };
      render();
      // Match the preview-success path: focus the first row so
      // 1-5 / Tab / Enter all work.
      requestAnimationFrame(() => {
        const first = document.querySelector('.tg-dedup-row');
        if (first) first.focus();
      });
      return;
    }
    state.quickAdd.error = (e && e.message) || 'Quick add failed';
    render();
    return;
  }
  // Close modal and surface a toast. If the operator is on Tasks,
  // refresh so the new row appears.
  const addedSummary = (snap && snap.summary) || '';
  state.quickAdd = {
    open: false, summary: '', businessSlug: '', dueAtLocal: '',
    description: '', kind: 'task', submitting: false, error: null,
    ownerUserId: '', members: [], membersLoading: false, dedup: null,
  };
  render();
  if (typeof tgShowToast === 'function') {
    tgShowToast(`Added "${addedSummary.slice(0, 60)}"`, 'ok');
  }
  if (state.view === 'tasks' && typeof refreshTasks === 'function') {
    try { await refreshTasks(); } catch (_e) {}
  }
  return result;
}

function renderTriageView() {
  const items = state.review.items || [];
  const counts = tgSubviewCounts();
  const subview = state.review.subview || 'inbox';

  // Visible items after sub-view filter + forecast filter + search
  let visible = tgSubviewItems();
  if (state.review.forecastFilter && (subview === 'inbox' || subview === 'stale')) {
    visible = visible.filter(i => {
      const ageDays = tgAgeMinutes(i) / (60 * 24);
      const f = state.review.forecastFilter;
      if (f === 'fresh')  return ageDays < 1;
      if (f === 'warm')   return ageDays >= 1 && ageDays < TG.staleDays;
      if (f === 'stale')  return ageDays >= TG.staleDays && ageDays < TG.vstaleDays;
      if (f === 'vstale') return ageDays >= TG.vstaleDays;
      if (f === 'urgent') return (i.proposed_action === 'AMBIGUOUS') ||
                                  (i.confidence != null && Number(i.confidence) < 0.55);
      return true;
    });
  }
  const q = (state.review.searchQuery || '').toLowerCase().trim();
  if (q) {
    visible = visible.filter(i => {
      const cf = i.candidate_facts || {};
      const summary = (cf.summary || '').toLowerCase();
      const quote = (cf.source_quote || '').toLowerCase();
      return summary.includes(q) || quote.includes(q);
    });
  }
  // Re-key state.review.items to the visible subset for keyboard nav
  // semantics (focusedIndex indexes into the rendered list).
  state.review.items_visible = visible;

  const focusedItem = state.review.focusedIndex != null
    ? visible[state.review.focusedIndex] || null
    : null;

  const detailOpen = state.review.detailOpen !== false;
  const wrapClass = detailOpen ? '' : 'no-detail';

  const listInner = visible.length === 0
    ? `<div class="tg-empty">No items in this view.</div>`
    : `
      <div class="tg-list-head">
        <div>Age</div><div>Action</div><div>Conf</div><div>Source</div>
        <div>Summary</div><div class="col-flags right">Flags</div>
      </div>
      ${visible.map((it, i) => tgRenderRow(it, i)).join('')}
    `;

  return `
    <div class="triage-view">
      <div class="tg-head">
        <div class="tg-head-title">
          <h1 class="tg-h1">Triage</h1>
          <div class="tg-head-sub">${counts.inbox} pending</div>
        </div>
        ${tgRenderSubtabs(counts)}
      </div>
      ${tgRenderForecast()}
      <div class="tg-listwrap ${wrapClass}">
        <div class="tg-list" tabindex="0">${listInner}</div>
        ${detailOpen ? tgRenderDetail(focusedItem) : ''}
      </div>
      ${tgRenderBulkBar()}
      ${tgRenderRejectModal()}
      ${tgRenderSnoozeModal()}
      ${tgRenderToast()}
    </div>
  `;
}

function tgShowToast(msg, kind) {
  state.review.toast = { msg, kind: kind || 'ok' };
  render();
  setTimeout(() => {
    if (state.review.toast && state.review.toast.msg === msg) {
      state.review.toast = null;
      render();
    }
  }, TG.toastMs);
}

// LEGACY shim — older code paths may still call renderReviewItem.
function renderReviewItem(item, index) {
  const expanded = state.review.expandedId === item.id;
  // Phase UI-1: keyboard focus + selection visuals
  const focused = state.review.focusedIndex === index;
  const selected = state.review.selectedIds.has(item.id);
  const detail = expanded ? state.review.expandedDetail : null;
  const action = item.proposed_action;
  const conf = (item.confidence != null) ? Number(item.confidence).toFixed(2) : '—';

  let detailBlock = '';
  if (expanded && detail) {
    const patch = detail.proposed_patch || {};
    const cf = detail.candidate_facts || {};
    const verrors = detail.validation_errors || [];
    const apply_err = detail.last_apply_error || {};
    const summaryLine = action === 'CREATE_TASK' && patch.create
      ? patch.create.summary
      : (action === 'UPDATE_TASK' && patch.update ? (patch.update.summary || cf.summary || '') : cf.summary || '');
    const businesses = (cf.businesses || []).join(', ');
    const verrorsBlock = verrors.length
      ? `<div class="rv-errors"><h3>Validation errors</h3><ul>${verrors.map(e =>
          `<li><code>${escapeHtml(e.code || '')}</code> — ${escapeHtml(e.message || '')}</li>`
        ).join('')}</ul></div>`
      : '';
    const applyErrBlock = (apply_err && Object.keys(apply_err).length)
      ? `<div class="rv-errors apply"><h3>Last apply error</h3>
           <div><code>${escapeHtml(apply_err.code || '')}</code></div>
           <pre>${escapeHtml(JSON.stringify(apply_err.detail || apply_err.errors || {}, null, 2))}</pre>
         </div>`
      : '';
    const reasonLine = detail.reason
      ? `<div class="rv-reason"><strong>LLM reason:</strong> ${escapeHtml(detail.reason)}</div>`
      : '';
    const targetLine = detail.target_task_id
      ? `<div>Target task: <code>${escapeHtml(detail.target_task_id)}</code> (base v${detail.base_task_version ?? '—'})</div>`
      : '';
    detailBlock = `
      <div class="rv-detail">
        <div class="rv-summary"><strong>${escapeHtml(action)}:</strong> ${escapeHtml(summaryLine)}</div>
        <div>Businesses: ${escapeHtml(businesses) || '<span class="muted">(none)</span>'}</div>
        ${targetLine}
        ${reasonLine}
        <details class="rv-raw"><summary>Raw proposed_patch</summary>
          <pre>${escapeHtml(JSON.stringify(patch, null, 2))}</pre>
        </details>
        <details class="rv-raw"><summary>Raw candidate_facts</summary>
          <pre>${escapeHtml(JSON.stringify(cf, null, 2))}</pre>
        </details>
        ${verrorsBlock}
        ${applyErrBlock}
        <div class="rv-actions">
          <button class="rv-approve" data-rid="${escapeHtml(item.id)}"
                  ${state.review.pendingAction ? 'disabled' : ''}>
            Approve
          </button>
          <button class="rv-reject" data-rid="${escapeHtml(item.id)}"
                  ${state.review.pendingAction ? 'disabled' : ''}>
            Reject
          </button>
          ${state.review.pendingAction
            ? `<span class="rv-pending muted">${escapeHtml(state.review.pendingAction)}…</span>`
            : ''}
        </div>
        ${state.review.actionError
          ? `<div class="error rv-action-err">${escapeHtml(state.review.actionError)}</div>`
          : ''}
      </div>
    `;
  }

  const statusClass = (item.status || 'pending').replace('_', '-');
  const focusClass = focused ? ' focused' : '';
  const selectedClass = selected ? ' selected' : '';
  const selectionToggle = selected
    ? '<span class="rv-checkbox checked" aria-label="selected">☑</span>'
    : '<span class="rv-checkbox" aria-label="not selected">☐</span>';
  return `
    <li class="rv-item ${statusClass}${expanded ? ' expanded' : ''}${focusClass}${selectedClass}"
        data-review-id="${escapeHtml(item.id)}"
        data-review-index="${index}">
      <div class="rv-item-header">
        ${selectionToggle}
        <span class="rv-action-pill ${escapeHtml(action.toLowerCase())}">${escapeHtml(action)}</span>
        <span class="rv-conf">conf ${conf}</span>
        <span class="rv-status ${statusClass}">${escapeHtml(item.status)}</span>
      </div>
      <div class="rv-item-meta">
        <code class="rv-id">${escapeHtml(item.id.slice(0, 8))}…</code>
        <span class="muted">${escapeHtml(fmtRelative(item.created_at))}</span>
      </div>
      ${detailBlock}
    </li>
  `;
}

function renderReviewList() {
  if (!state.review.items.length) {
    return `<div class="empty-state">No items in the review queue.</div>`;
  }
  const selCount = state.review.selectedIds.size;
  const totalLine = `<div class="task-count">${state.review.total} review item${state.review.total === 1 ? '' : 's'}${selCount ? ` <span class="rv-sel-count">· ${selCount} selected</span>` : ''}</div>`;
  // Sticky bottom bar appears when ≥1 selected (Phase UI-1 partial — bulk
  // approve/reject only; snooze + edit-then-approve land in UI-2).
  const bulkBar = selCount > 0
    ? `<div class="rv-bulk-bar">
         <span class="rv-bulk-count"><strong>${selCount}</strong> selected</span>
         <button class="rv-bulk-approve" ${state.review.bulkInProgress ? 'disabled' : ''}>Approve all (1)</button>
         <button class="rv-bulk-reject" ${state.review.bulkInProgress ? 'disabled' : ''}>Reject all (2)</button>
         <button class="rv-bulk-clear">Clear (Esc)</button>
       </div>`
    : '';
  return `
    ${totalLine}
    <ul class="rv-list">${state.review.items.map((item, i) => renderReviewItem(item, i)).join('')}</ul>
    ${bulkBar}
  `;
}

function renderSopFilters() {
  const businessOptions = [
    `<option value="all"${state.sops.businessFilter === 'all' ? ' selected' : ''}>All businesses</option>`,
    ...state.businesses.map(b =>
      `<option value="${escapeHtml(b.slug)}"${state.sops.businessFilter === b.slug ? ' selected' : ''}>${escapeHtml(b.name)}</option>`
    ),
  ].join('');
  return `
    <div class="filters">
      <div class="status-tabs">
        <button class="tab sop-status-tab${state.sops.statusFilter === 'active' ? ' active' : ''}" data-sop-status="active">Active</button>
        <button class="tab sop-status-tab${state.sops.statusFilter === 'archived' ? ' active' : ''}" data-sop-status="archived">Archived</button>
        <button class="tab sop-status-tab${state.sops.statusFilter === 'all' ? ' active' : ''}" data-sop-status="all">All</button>
      </div>
      <select class="business-filter" id="sop-business-filter">${businessOptions}</select>
    </div>
  `;
}

function renderSopTemplateEditor() {
  // Inline editor for state.sops.editing.templates. seq_no = array index.
  const templates = state.sops.editing.templates || [];
  const saveDisabled = state.sops.editing.pending ? 'disabled' : '';
  const rows = templates.map((t, idx) => `
    <li class="sop-template-edit" data-edit-idx="${idx}">
      <div class="sop-template-edit-head">
        <span class="seq">${idx}</span>
        <input class="t-summary" data-field="summary" type="text" maxlength="4096"
               placeholder="Summary (required)" value="${escapeHtml(t.summary || '')}">
        <button class="t-remove" data-edit-idx="${idx}" title="Remove">×</button>
      </div>
      <div class="sop-template-edit-row">
        <input class="t-offset" data-field="due_offset_days" type="number"
               min="-3650" max="3650" placeholder="Days from anchor"
               value="${t.due_offset_days == null ? '' : escapeHtml(String(t.due_offset_days))}">
        <input class="t-category" data-field="category" type="text" maxlength="64"
               placeholder="Category" value="${escapeHtml(t.category || '')}">
        <input class="t-priority" data-field="priority" type="text" maxlength="32"
               placeholder="Priority" value="${escapeHtml(t.priority || '')}">
        <input class="t-owner-role" data-field="owner_role" type="text" maxlength="64"
               placeholder="Owner role" value="${escapeHtml(t.owner_role || '')}">
      </div>
      <input class="t-dep" data-field="dependency_text" type="text" maxlength="2048"
             placeholder="Dependency text (optional)" value="${escapeHtml(t.dependency_text || '')}">
      <textarea class="t-desc" data-field="description" rows="2" maxlength="8192"
                placeholder="Description (optional)">${escapeHtml(t.description || '')}</textarea>
    </li>
  `).join('');

  const errorBlock = state.sops.editing.saveError
    ? `<div class="error sop-edit-err">${escapeHtml(state.sops.editing.saveError)}</div>`
    : '';
  const publishErrBlock = state.sops.editing.publishError
    ? `<div class="error sop-edit-err">${escapeHtml(state.sops.editing.publishError)}</div>`
    : '';

  return `
    <div class="sop-edit-pane">
      ${errorBlock}
      ${publishErrBlock}
      <ul class="sop-template-edit-list">${rows}</ul>
      <div class="sop-edit-actions">
        <button class="sop-edit-add-row" ${saveDisabled}>+ Add row</button>
        <button class="sop-edit-save" ${saveDisabled}>${state.sops.editing.pending === 'save' ? 'Saving…' : 'Save templates'}</button>
        <button class="sop-edit-publish" ${saveDisabled}>${state.sops.editing.pending === 'publish' ? 'Publishing…' : 'Publish'}</button>
        <button class="sop-edit-cancel" ${saveDisabled}>Discard edits</button>
      </div>
    </div>
  `;
}

function renderSopVersionRow(version, expanded) {
  const stateClass = (version.state || 'draft').replace('_', '-');
  const publishedLine = version.published_at
    ? `<span class="muted">published ${escapeHtml(fmtRelative(version.published_at))}</span>`
    : '';
  // Editor is scoped to (sopId, versionNo). state.sops.expandedId is
  // the SOP whose detail block this row renders inside.
  const isEditing = state.sops.editing
                  && state.sops.editing.sopId === state.sops.expandedId
                  && state.sops.editing.versionNo === version.version_no;
  let templatesBlock = '';
  if (expanded && isEditing) {
    templatesBlock = renderSopTemplateEditor();
  } else if (expanded) {
    const detail = state.sops.selectedVersionDetail;
    if (!detail) {
      templatesBlock = `<div class="sop-templates muted">Loading templates…</div>`;
    } else {
      const templates = detail.template_tasks || [];
      if (!templates.length) {
        templatesBlock = `<div class="sop-templates"><span class="muted">No templates in this version.</span></div>`;
      } else {
        const rows = templates.map(t => `
          <li class="sop-template">
            <div class="sop-template-head">
              <span class="seq">${escapeHtml(String(t.seq_no))}</span>
              <span class="summary">${escapeHtml(t.summary)}</span>
              ${t.priority ? `<span class="priority-pill">${escapeHtml(t.priority)}</span>` : ''}
            </div>
            <div class="sop-template-meta muted">
              ${t.due_offset_days != null ? `due ${escapeHtml(String(t.due_offset_days))}d` : 'no offset'}
              ${t.category ? ` · ${escapeHtml(t.category)}` : ''}
              ${t.owner_role ? ` · role ${escapeHtml(t.owner_role)}` : ''}
              ${t.dependency_text ? ` · ⏸ ${escapeHtml(t.dependency_text)}` : ''}
            </div>
            ${t.description ? `<div class="sop-template-desc">${escapeHtml(t.description)}</div>` : ''}
          </li>
        `).join('');
        templatesBlock = `
          <div class="sop-templates">
            <h4>Templates (${templates.length})</h4>
            <ul class="sop-template-list">${rows}</ul>
          </div>
        `;
      }
    }
  }
  // Per-version "Edit draft" button — only shown for draft versions
  // when not already editing.
  const editButton = (expanded && version.state === 'draft' && !isEditing)
    ? `<button class="sop-version-edit" data-version-no="${escapeHtml(String(version.version_no))}">Edit draft</button>`
    : '';
  return `
    <li class="sop-version ${stateClass}${expanded ? ' expanded' : ''}${isEditing ? ' editing' : ''}" data-version-no="${escapeHtml(String(version.version_no))}">
      <div class="sop-version-head">
        <span class="version-no">v${escapeHtml(String(version.version_no))}</span>
        <span class="state-pill ${stateClass}">${escapeHtml(version.state)}</span>
        ${publishedLine}
        ${editButton}
      </div>
      ${version.change_log ? `<div class="sop-version-changelog">${escapeHtml(version.change_log)}</div>` : ''}
      ${templatesBlock}
    </li>
  `;
}

function renderSopItem(sop) {
  const expanded = state.sops.expandedId === sop.id;
  const detail = expanded ? state.sops.expandedDetail : null;
  const businessName = (state.businesses.find(b => b.id === sop.business_id) || {}).name || sop.business_id;
  const latestLabel = sop.latest_version_no != null
    ? `latest v${sop.latest_version_no}`
    : '<span class="muted">no published version</span>';
  let detailBlock = '';
  if (expanded) {
    if (state.sops.loadError) {
      detailBlock = `<div class="error">${escapeHtml(state.sops.loadError)}</div>`;
    } else if (!detail) {
      detailBlock = `<div class="muted">Loading versions…</div>`;
    } else {
      const versions = detail.versions || [];
      const versionRows = versions.length
        ? versions.map(v =>
            renderSopVersionRow(v, state.sops.selectedVersionNo === v.version_no)
          ).join('')
        : '<li class="muted">No versions yet.</li>';
      // "Create draft" button: visible when this SOP has no
      // outstanding draft and the SOP is active. The schema's
      // one-draft-per-sop partial UNIQUE rejects a second concurrent
      // create with 409, so the UI gates it visually as well.
      const hasDraft = versions.some(v => v.state === 'draft');
      const isActive = (detail.sop.status === 'active');
      const editing = !!state.sops.editing;
      const createDraftBtn = (isActive && !hasDraft && !editing)
        ? `<button class="sop-create-draft" data-sop-id="${escapeHtml(detail.sop.id)}">+ Create draft version</button>`
        : '';
      detailBlock = `
        <div class="sop-detail">
          ${detail.sop.description ? `<div class="sop-description">${escapeHtml(detail.sop.description)}</div>` : ''}
          <div class="sop-detail-actions">${createDraftBtn}</div>
          <h3>Versions</h3>
          <ul class="sop-version-list">${versionRows}</ul>
        </div>
      `;
    }
  }
  return `
    <li class="sop-item ${escapeHtml(sop.status)}${expanded ? ' expanded' : ''}" data-sop-id="${escapeHtml(sop.id)}">
      <div class="sop-item-head">
        <strong>${escapeHtml(sop.name)}</strong>
        <span class="biz-pill">${escapeHtml(businessName)}</span>
        <span class="state-pill ${escapeHtml(sop.status)}">${escapeHtml(sop.status)}</span>
        <span class="muted sop-latest">${latestLabel}</span>
      </div>
      ${detailBlock}
    </li>
  `;
}

function renderSopList() {
  // Surface a top-level load error so a /v1/sops failure doesn't
  // silently render as the empty state. refreshSops sets
  // state.sops.loadError; this is the matching display path.
  if (state.sops.loadError && !state.sops.items.length) {
    return `<div class="error sop-load-error">${escapeHtml(state.sops.loadError)}</div>`;
  }
  if (!state.sops.items.length) {
    return `<div class="empty-state">No SOPs match the current filters.</div>`;
  }
  const errorBanner = state.sops.loadError
    ? `<div class="error sop-load-error">${escapeHtml(state.sops.loadError)}</div>`
    : '';
  return `
    ${errorBanner}
    <div class="task-count">${state.sops.total} SOP${state.sops.total === 1 ? '' : 's'}</div>
    <ul class="sop-list">${state.sops.items.map(renderSopItem).join('')}</ul>
  `;
}

// ---------- Create SOP form (UI 3/3) ----------

function renderCreateSopForm() {
  const c = state.sops.creating;
  if (!c) {
    return `
      <div class="sop-create-bar">
        <button class="sop-create-open">+ New SOP</button>
      </div>
    `;
  }
  const businessOptions = state.businesses.map(b => `
    <option value="${escapeHtml(b.slug)}"${c.businessSlug === b.slug ? ' selected' : ''}>${escapeHtml(b.name)}</option>
  `).join('');
  const errorBlock = c.error
    ? `<div class="error sop-edit-err">${escapeHtml(c.error)}</div>`
    : '';
  const pendingDisabled = c.pending ? 'disabled' : '';
  return `
    <div class="sop-create-form">
      ${errorBlock}
      <div class="sop-create-row">
        <select id="sop-create-biz" ${pendingDisabled}>
          <option value="">Select business…</option>
          ${businessOptions}
        </select>
        <input id="sop-create-name" type="text" maxlength="256"
               placeholder="SOP name (required)" value="${escapeHtml(c.name || '')}" ${pendingDisabled}>
      </div>
      <textarea id="sop-create-desc" rows="2" maxlength="8192"
                placeholder="Description (optional)" ${pendingDisabled}>${escapeHtml(c.description || '')}</textarea>
      <div class="sop-create-actions">
        <button class="sop-create-submit" ${pendingDisabled}>${c.pending ? 'Creating…' : 'Create SOP'}</button>
        <button class="sop-create-cancel" ${pendingDisabled}>Cancel</button>
      </div>
    </div>
  `;
}

// ---------- Anchors section (UI 3/3) ----------

function renderAnchorsSection() {
  const a = state.anchors;
  if (!a.expanded) {
    return `
      <div class="anchors-toggle">
        <button class="anchors-open">Show anchors (${a.total})</button>
      </div>
    `;
  }
  // Filter row.
  const businessOptions = [
    `<option value="all"${a.businessFilter === 'all' ? ' selected' : ''}>All businesses</option>`,
    ...state.businesses.map(b =>
      `<option value="${escapeHtml(b.slug)}"${a.businessFilter === b.slug ? ' selected' : ''}>${escapeHtml(b.name)}</option>`
    ),
  ].join('');
  const fireResult = a.fireResult
    ? `<div class="fire-result">
         Fired anchor — ${escapeHtml(String(a.fireResult.reviewItemsCreated))} review items created.
         <button class="goto-review">Go to Review</button>
       </div>`
    : '';
  const loadErr = a.loadError
    ? `<div class="error">${escapeHtml(a.loadError)}</div>`
    : '';
  const items = a.items.length
    ? `<ul class="anchor-list">${a.items.map(renderAnchorRow).join('')}</ul>`
    : `<div class="empty-state">No anchors match the current filters.</div>`;
  const scheduleForm = renderScheduleAnchorForm();
  return `
    <div class="anchors-section">
      <div class="anchors-head">
        <h2>Anchors</h2>
        <button class="anchors-close">Hide</button>
      </div>
      <div class="filters">
        <div class="status-tabs">
          <button class="tab anchor-status-tab${a.statusFilter === 'scheduled' ? ' active' : ''}" data-anchor-status="scheduled">Scheduled</button>
          <button class="tab anchor-status-tab${a.statusFilter === 'fired' ? ' active' : ''}" data-anchor-status="fired">Fired</button>
          <button class="tab anchor-status-tab${a.statusFilter === 'cancelled' ? ' active' : ''}" data-anchor-status="cancelled">Cancelled</button>
          <button class="tab anchor-status-tab${a.statusFilter === 'all' ? ' active' : ''}" data-anchor-status="all">All</button>
        </div>
        <select class="business-filter" id="anchor-business-filter">${businessOptions}</select>
      </div>
      ${fireResult}
      ${loadErr}
      ${scheduleForm}
      ${items}
    </div>
  `;
}

function renderAnchorRow(anchor) {
  const stateClass = (anchor.state || 'scheduled');
  const sopName = (() => {
    const s = state.sops.items.find(x => x.id === anchor.sop_id);
    return s ? s.name : anchor.sop_id;
  })();
  const businessName = (state.businesses.find(b => b.id === anchor.business_id) || {}).name || anchor.business_id;
  const fireBtn = (anchor.state === 'scheduled')
    ? `<button class="anchor-fire" data-anchor-id="${escapeHtml(anchor.id)}">Fire</button>`
    : '';
  return `
    <li class="anchor-row ${stateClass}" data-anchor-id="${escapeHtml(anchor.id)}">
      <div class="anchor-head">
        <span class="anchor-kind">${escapeHtml(anchor.kind)}</span>
        <span class="biz-pill">${escapeHtml(businessName)}</span>
        <span class="state-pill ${stateClass}">${escapeHtml(anchor.state)}</span>
        ${fireBtn}
      </div>
      <div class="anchor-meta muted">
        <span>${escapeHtml(sopName)}</span>
        <span>scheduled ${escapeHtml(fmtDate(anchor.scheduled_for))}</span>
        ${anchor.fired_at ? ` · fired ${escapeHtml(fmtRelative(anchor.fired_at))}` : ''}
      </div>
      ${anchor.notes ? `<div class="anchor-notes">${escapeHtml(anchor.notes)}</div>` : ''}
    </li>
  `;
}

function renderScheduleAnchorForm() {
  const s = state.anchors.scheduling;
  if (!s) {
    return `
      <div class="schedule-bar">
        <button class="anchor-schedule-open">+ Schedule anchor</button>
      </div>
    `;
  }
  const businessOptions = [
    `<option value="">Select business…</option>`,
    ...state.businesses.map(b =>
      `<option value="${escapeHtml(b.slug)}"${s.businessSlug === b.slug ? ' selected' : ''}>${escapeHtml(b.name)}</option>`
    ),
  ].join('');
  const sopOptionsHtml = (s.sopOptions || []).map(o =>
    `<option value="${escapeHtml(o.id)}"${s.sopId === o.id ? ' selected' : ''}>${escapeHtml(o.name)}</option>`
  ).join('');
  const errorBlock = s.error
    ? `<div class="error sop-edit-err">${escapeHtml(s.error)}</div>`
    : '';
  const pendingDisabled = s.pending ? 'disabled' : '';
  return `
    <div class="anchor-schedule-form">
      ${errorBlock}
      <div class="sop-create-row">
        <select id="anchor-sched-biz" ${pendingDisabled}>${businessOptions}</select>
        <select id="anchor-sched-sop" ${pendingDisabled}>
          <option value="">${s.businessSlug ? 'Select SOP…' : 'Pick a business first'}</option>
          ${sopOptionsHtml}
        </select>
      </div>
      <div class="sop-create-row">
        <input id="anchor-sched-kind" type="text" maxlength="64"
               placeholder="Kind (e.g. redhot_opening)" value="${escapeHtml(s.kind || '')}" ${pendingDisabled}>
        <input id="anchor-sched-when" type="datetime-local"
               value="${escapeHtml(s.scheduledForLocal || '')}" ${pendingDisabled}>
      </div>
      <textarea id="anchor-sched-notes" rows="2" maxlength="4096"
                placeholder="Notes (optional)" ${pendingDisabled}>${escapeHtml(s.notes || '')}</textarea>
      <div class="sop-create-actions">
        <button class="anchor-sched-submit" ${pendingDisabled}>${s.pending ? 'Scheduling…' : 'Schedule'}</button>
        <button class="anchor-sched-cancel" ${pendingDisabled}>Cancel</button>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Phase UI-2A app shell — left rail + topbar wraps every view.
// ---------------------------------------------------------------------------

function renderRail() {
  const p = state.principal || {};
  const isAdmin = _isPlatformAdmin(p);
  const isUserPrincipal = p.principal_type === 'user';
  const v = state.view || 'tasks';
  const inboxCount = (function () {
    try {
      const items = (state.review && state.review.items) || [];
      return items.filter(i => i.status === 'pending').length || 0;
    } catch (_e) { return 0; }
  })();
  const initial = (p.display_name || p.email || '?').slice(0, 1).toUpperCase();

  // Inline SVG icons — kept tiny on purpose; design-system polish later.
  const ico = {
    triage: '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M3 12h12M3 18h18"/></svg>',
    tasks:  '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
    sops:   '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M9 14h6"/><path d="M9 18h6"/></svg>',
    set:    '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  };
  const item = (view, label, icon, badge) => `
    <button class="view-tab tg-rail-item${v === view ? ' active' : ''}" data-view="${view}">
      ${icon}
      <span>${escapeHtml(label)}</span>
      ${badge ? `<span class="badge">${badge}</span>` : ''}
    </button>`;
  return `
    <aside class="tg-rail">
      <div class="tg-rail-brand"><span class="dot"></span><span>OpsMemory</span></div>
      <div class="tg-rail-sec">Workspace</div>
      ${isAdmin ? item('review', 'Triage', ico.triage, inboxCount > 0 ? inboxCount : '') : ''}
      ${item('tasks', 'Tasks', ico.tasks)}
      ${isAdmin ? item('sops', 'SOPs', ico.sops) : ''}
      <div class="tg-rail-sec">Account</div>
      ${isUserPrincipal ? item('settings', 'Settings', ico.set) : ''}
      <div class="tg-rail-foot">
        <span class="tg-avatar">${escapeHtml(initial)}</span>
        <div class="who-block">
          <div class="who-name">${escapeHtml(p.display_name || 'Signed in')}</div>
          <div class="who-meta">${escapeHtml(p.email || '')}</div>
        </div>
      </div>
    </aside>`;
}

function renderBottomNav() {
  const p = state.principal || {};
  const isAdmin = _isPlatformAdmin(p);
  const isUserPrincipal = p.principal_type === 'user';
  const v = state.view || 'tasks';
  const inboxCount = (function () {
    try {
      const items = (state.review && state.review.items) || [];
      return items.filter(i => i.status === 'pending').length || 0;
    } catch (_e) { return 0; }
  })();
  const ico = {
    triage: '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M3 12h12M3 18h18"/></svg>',
    tasks:  '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
    sops:   '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    set:    '<svg class="tg-rail-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 1v6M12 17v6M4.22 4.22l4.24 4.24M15.54 15.54l4.24 4.24M1 12h6M17 12h6M4.22 19.78l4.24-4.24M15.54 8.46l4.24-4.24"/></svg>',
  };
  const bn = (view, label, icon, badge) => `
    <button class="view-tab tg-bn-item${v === view ? ' active' : ''}" data-view="${view}">
      <span style="position:relative">${icon}${badge ? `<span class="badge">${badge}</span>` : ''}</span>
      <span>${escapeHtml(label)}</span>
    </button>`;
  return `
    <nav class="tg-bottomnav">
      <div class="tg-bottomnav-inner">
        ${isAdmin ? bn('review', 'Triage', ico.triage, inboxCount > 0 ? inboxCount : '') : ''}
        ${bn('tasks', 'Tasks', ico.tasks)}
        ${isAdmin ? bn('sops', 'SOPs', ico.sops) : ''}
        ${isUserPrincipal ? bn('settings', 'Settings', ico.set) : ''}
      </div>
    </nav>`;
}

function renderTopbar() {
  const p = state.principal || {};
  const v = state.view || 'tasks';
  const labels = { review: 'Triage', tasks: 'Tasks', sops: 'SOPs', settings: 'Settings' };
  const allBiz = (p.businesses || []);
  const businesses = allBiz
    .slice(0, 3)
    .map(b => `<span class="tg-biz-pill">${escapeHtml(b.name)}</span>`)
    .join('');
  const moreBiz = allBiz.length > 3
    ? `<span class="tg-biz-pill" title="${escapeHtml(allBiz.slice(3).map(b => b.name).join(', '))}">+${allBiz.length - 3}</span>`
    : '';
  const rolePill = p.role
    ? `<span class="tg-biz-pill" title="Role">${escapeHtml(p.role)}</span>`
    : '';
  const syncBadge = (typeof renderSyncBadge === 'function') ? renderSyncBadge() : '';
  return `
    <header class="tg-topbar">
      <div class="tg-breadcrumb">
        <span>Workspace</span>
        <span class="sep">›</span>
        <b>${escapeHtml(labels[v] || 'Workspace')}</b>
      </div>
      <div class="grow"></div>
      <span class="sync-slot">${syncBadge}</span>
      ${rolePill}
      ${businesses}${moreBiz}
      <button class="tg-iconbtn tg-iconbtn-add" id="tg-add-btn" title="Quick add (Q)" aria-label="Quick add">+</button>
      <button class="tg-iconbtn" id="tg-help-btn" title="Keyboard shortcuts (?)" aria-label="Shortcuts">?</button>
    </header>`;
}

function renderAppShell(viewContent) {
  // Codex B3-3 blocker: when the Quick Add dedup confirm is open,
  // mark .tg-app inert so Tab cannot escape into the underlying
  // app chrome (rail, topbar, FAB). Quick Add + dedup are rendered
  // into #tg-quickadd-host outside #root, so root re-renders do not
  // destroy the active compose input.
  const dedupOpen = !!(state.quickAdd && state.quickAdd.open
                       && state.quickAdd.dedup);
  const appInert = dedupOpen ? 'inert aria-hidden="true"' : '';
  return `
    <div class="tg-app" ${appInert}>
      ${renderRail()}
      <div class="tg-main">
        ${renderTopbar()}
        <div class="tg-content">${viewContent}</div>
      </div>
      ${renderBottomNav()}
      <button class="tg-fab" id="tg-fab" aria-label="Quick add (Q)" title="Quick add (Q)">+</button>
    </div>`;
}

function render() {
  const root = document.getElementById('root');
  if (!root) return;
  // B4 blocker: capture edit-form focus + caret BEFORE root.innerHTML
  // wipes the DOM, so we can restore it AFTER. Without this, async
  // re-render triggers (replayOutbox 10s tick) destroy the focused
  // input and the operator's keystrokes start hitting body, where
  // global 1/2 shortcuts can fire and approve/reject the wrong row.
  let _editFocusSnapshot = null;
  if (state.review && state.review.editingId) {
    const ae = document.activeElement;
    if (ae && ae.id && ae.id.startsWith('tg-edit-')) {
      _editFocusSnapshot = {
        id: ae.id,
        selStart: typeof ae.selectionStart === 'number' ? ae.selectionStart : null,
        selEnd: typeof ae.selectionEnd === 'number' ? ae.selectionEnd : null,
      };
    }
  }
  // The shell takes over the viewport — root needs to give up its
  // legacy max-width / centering. Adding the class flips that.
  root.classList.add('has-shell');

  let viewContent;
  if (state.view === 'review') {
    viewContent = `<div id="review-area">${renderTriageView()}</div>`;
  } else if (state.view === 'sops') {
    viewContent = `
      <div class="tg-page">
        <h1 class="tg-page-h1">SOPs</h1>
        ${renderSopFilters()}
        ${renderCreateSopForm()}
        <div id="sops-area">${renderSopList()}</div>
        ${renderAnchorsSection()}
      </div>`;
  } else if (state.view === 'settings') {
    viewContent = `
      <div class="tg-page">
        <h1 class="tg-page-h1">Settings</h1>
        <div id="settings-area">${renderSettings()}</div>
      </div>`;
  } else {
    viewContent = `
      <div class="tg-page">
        <h1 class="tg-page-h1">Tasks</h1>
        ${renderDashboardTiles()}
        ${renderFilters()}
        <div id="task-area">${renderTaskList()}</div>
      </div>`;
  }
  root.innerHTML = renderAppShell(viewContent);
  attachEventHandlers();
  _tgRenderQuickAddHost();
  // Restore edit-form focus + caret if we snapshotted before render.
  if (_editFocusSnapshot) {
    requestAnimationFrame(() => {
      const el = document.getElementById(_editFocusSnapshot.id);
      if (el) {
        el.focus();
        try {
          if (_editFocusSnapshot.selStart != null
              && _editFocusSnapshot.selEnd != null) {
            el.setSelectionRange(_editFocusSnapshot.selStart,
                                 _editFocusSnapshot.selEnd);
          }
        } catch (_e) {}
      }
    });
  }
}

function renderError(err) {
  const root = document.getElementById('root');
  if (!root) return;
  root.innerHTML = `<div class="error">${escapeHtml(err.message || 'Unknown error.')}</div>`;
}

// ---------------------------------------------------------------------------
// Chunk 10 step 3 sub-(a) — Settings tab (read-only).
// ---------------------------------------------------------------------------

// Safe Notification.permission read. Codex chunk-10-step3a (2):
// using a bare `Notification` reference throws ReferenceError on
// browsers without the API; gate on `'Notification' in window`.
function readNotificationPermission() {
  if (typeof window !== 'undefined'
      && 'Notification' in window
      && typeof window.Notification.permission === 'string') {
    return window.Notification.permission;
  }
  return 'unsupported';
}

function detectPushApiAvailable() {
  return (typeof window !== 'undefined'
          && 'PushManager' in window
          && typeof navigator !== 'undefined'
          && 'serviceWorker' in navigator
          && 'Notification' in window);
}

// Codex chunk-10-step3a (3): wrap navigator.serviceWorker.ready in
// a timeout. The browser can hang indefinitely if registration is
// late or fails; the Settings shell must not block on it.
async function _readServiceWorkerSubscription(timeoutMs) {
  if (!('serviceWorker' in navigator)) {
    return { ready: false, subscription: null };
  }
  let timeoutHandle = null;
  const readyPromise = navigator.serviceWorker.ready.then(reg => {
    if (!reg || !reg.pushManager) return { ready: true, subscription: null };
    return reg.pushManager.getSubscription().then(sub => ({
      ready: true, subscription: sub,
    }));
  });
  const timeoutPromise = new Promise(resolve => {
    timeoutHandle = setTimeout(
      () => resolve({ ready: false, subscription: null, timedOut: true }),
      timeoutMs,
    );
  });
  try {
    const result = await Promise.race([readyPromise, timeoutPromise]);
    // Codex chunk-10-step3a-close: clear the late timer so it
    // doesn't fire after a winning ready.
    if (timeoutHandle != null) clearTimeout(timeoutHandle);
    return result;
  } catch (err) {
    if (timeoutHandle != null) clearTimeout(timeoutHandle);
    return { ready: false, subscription: null, error: err };
  }
}

async function loadNotificationPrefs() {
  return await api('/v1/notifications/prefs');
}

async function loadPushSubscriptions() {
  return await api('/v1/notifications/web_push/subscriptions');
}

async function patchNotificationPref(channel, body) {
  return await api(`/v1/notifications/prefs/${encodeURIComponent(channel)}`, {
    method: 'PATCH',
    body,
  });
}

async function deletePushSubscription(id) {
  return await api(`/v1/notifications/web_push/subscriptions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
}

async function postPushSubscription(body) {
  return await api('/v1/notifications/web_push/subscriptions', {
    method: 'POST',
    body,
  });
}

async function postTestPush(subscription_id) {
  return await api('/v1/notifications/web_push/test', {
    method: 'POST',
    body: { subscription_id },
  });
}

// Standard Web Push helper: convert a base64url-encoded VAPID
// public key into the Uint8Array PushManager.subscribe expects.
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function _b64FromArrayBuffer(buf) {
  if (!buf) return null;
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.byteLength; i++) s += String.fromCharCode(bytes[i]);
  // base64url, no padding (matches what the server's CHECK
  // expects on web_push_subscriptions p256dh_key/auth_key).
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function _extractSubscriptionKeys(sub) {
  // Prefer .toJSON() when it returns the keys. Some browsers
  // (older Safari) only expose them via getKey().
  const json = (typeof sub.toJSON === 'function') ? sub.toJSON() : null;
  if (json && json.keys && json.keys.p256dh && json.keys.auth) {
    return { p256dh: json.keys.p256dh, auth: json.keys.auth };
  }
  if (typeof sub.getKey === 'function') {
    return {
      p256dh: _b64FromArrayBuffer(sub.getKey('p256dh')),
      auth: _b64FromArrayBuffer(sub.getKey('auth')),
    };
  }
  return { p256dh: null, auth: null };
}

async function loadVapidPublicKey() {
  // Treat 503 as "server doesn't have VAPID configured" — that's a
  // valid state, not an error. Per
  // api/app/v1_notifications.py vapid_public 503 contract.
  try {
    const r = await api('/v1/notifications/vapid_public');
    return r.public_key || null;
  } catch (err) {
    if (err && err.status === 503) return null;
    throw err;
  }
}

async function refreshSettings() {
  state.settings.loading = true;
  state.settings.loadError = null;
  // Codex chunk-10-step3b2: a fresh refresh clears the revoke
  // error pill (any pending revokes keep their per-row pending
  // flag and complete independently).
  state.settings.subscriptionRevokeError = null;

  state.settings.pushApiAvailable = detectPushApiAvailable();
  state.settings.permissionState = readNotificationPermission();

  // Run the three API loads concurrently so a slow network doesn't
  // serialize them. SW probe runs in parallel too with its own
  // timeout.
  const apiPromises = Promise.all([
    loadNotificationPrefs(),
    loadPushSubscriptions(),
    loadVapidPublicKey(),
  ]);
  const swPromise = state.settings.pushApiAvailable
    ? _readServiceWorkerSubscription(2000)
    : Promise.resolve({ ready: false, subscription: null });

  try {
    const [[prefsResp, subsResp, vapidPub], swResult] = await Promise.all([
      apiPromises, swPromise,
    ]);
    state.settings.prefs = (prefsResp && prefsResp.items) || [];
    state.settings.subscriptions = (subsResp && subsResp.items) || [];
    state.settings.vapidPublicKey = vapidPub;
    state.settings.serviceWorkerReady = !!swResult.ready;
    state.settings.browserSubscription = swResult.subscription || null;
    state.settings.loaded = true;
  } catch (err) {
    state.settings.loadError = (err && err.message) || 'Failed to load settings.';
  } finally {
    state.settings.loading = false;
    render();
  }
}

// ---------------------------------------------------------------------------
// Sub-(b) commit 1: prefs editor — draft helpers, validate, serialize.
// ---------------------------------------------------------------------------

const _SETTINGS_KIND_OPTIONS = ['daily', 'weekly'];
const _SETTINGS_WEEKDAYS = ['mon','tue','wed','thu','fri','sat','sun'];

function _seedSettingsDraft(pref) {
  // Build a draft from a pref row. Works for both saved rows and
  // synthesized defaults. Codex chunk-10-step3b1 (5): unknown kind
  // is rendered read-only / not editable; we don't coerce it here.
  const sched = (pref && pref.schedule) || {};
  const kind = _SETTINGS_KIND_OPTIONS.includes(sched.kind) ? sched.kind : 'daily';
  return {
    enabled: !!pref.enabled,
    scheduleKind: kind,
    hour: Number.isInteger(sched.hour) ? sched.hour : 7,
    minute: Number.isInteger(sched.minute) ? sched.minute : 0,
    weekday: _SETTINGS_WEEKDAYS.includes(sched.weekday) ? sched.weekday : 'mon',
    timezone: (typeof sched.timezone === 'string' && sched.timezone) ? sched.timezone : 'America/Phoenix',
    dirty: false,
    pending: false,
    error: null,
  };
}

function _validateSettingsDraft(draft) {
  if (typeof draft.enabled !== 'boolean') return 'enabled must be boolean';
  if (!_SETTINGS_KIND_OPTIONS.includes(draft.scheduleKind)) return 'kind must be daily or weekly';
  if (!Number.isInteger(draft.hour) || draft.hour < 0 || draft.hour > 23) return 'hour must be 0..23';
  if (!Number.isInteger(draft.minute) || draft.minute < 0 || draft.minute > 59) return 'minute must be 0..59';
  if (draft.scheduleKind === 'weekly' && !_SETTINGS_WEEKDAYS.includes(draft.weekday)) return 'weekday required for weekly';
  return null;
}

function _serializeSettingsDraft(draft) {
  const schedule = {
    kind: draft.scheduleKind,
    hour: draft.hour,
    minute: draft.minute,
    timezone: draft.timezone || 'America/Phoenix',
  };
  if (draft.scheduleKind === 'weekly') schedule.weekday = draft.weekday;
  return { enabled: draft.enabled, schedule };
}

function hasDirtySettingsDraft() {
  const e = state.settings.editing || {};
  return Object.values(e).some(d => d && d.dirty && !d.pending);
}

function hasPendingSettingsDraft() {
  const e = state.settings.editing || {};
  return Object.values(e).some(d => d && d.pending);
}

function _shortenKey(key) {
  // Codex chunk-10-step3a (5): show first 8 + ellipsis + last 4.
  // Don't call it a 'fingerprint' (we're not hashing). Public key
  // is non-secret, but rendering 88 base64 chars in the UI is noise.
  if (!key || typeof key !== 'string') return '';
  if (key.length <= 16) return key;
  return key.slice(0, 8) + '…' + key.slice(-4);
}

function _renderPermissionPill(state_value) {
  const cls = {
    'granted': 'badge-granted',
    'denied':  'badge-denied',
    'default': 'badge-default',
    'unsupported': 'badge-default',
  }[state_value] || 'badge-default';
  return `<span class="settings-pill ${cls}">${escapeHtml(state_value)}</span>`;
}

function _renderScheduleSummary(schedule) {
  if (!schedule || typeof schedule !== 'object') return '<span class="muted">—</span>';
  const kind = schedule.kind || 'unknown';
  const tz = schedule.timezone || '';
  if (kind === 'daily') {
    const h = (schedule.hour != null) ? String(schedule.hour).padStart(2, '0') : '??';
    const m = (schedule.minute != null) ? String(schedule.minute).padStart(2, '0') : '00';
    return escapeHtml(`daily ${h}:${m} ${tz}`);
  }
  if (kind === 'weekly') {
    // Backend contract: schedule.weekday is a single string
    // ('mon'|'tue'|...) per api/app/v1_notifications.py _DEFAULT_PREFS.
    // Codex chunk-10-step3a-close (2): match the actual contract.
    const weekday = (typeof schedule.weekday === 'string') ? schedule.weekday : '???';
    const h = (schedule.hour != null) ? String(schedule.hour).padStart(2, '0') : '??';
    const m = (schedule.minute != null) ? String(schedule.minute).padStart(2, '0') : '00';
    return escapeHtml(`weekly ${weekday} ${h}:${m} ${tz}`);
  }
  return escapeHtml(JSON.stringify(schedule));
}

function _renderSettingsLine(label, value) {
  return `<div class="settings-row"><span class="settings-label">${escapeHtml(label)}</span><span class="settings-value">${value}</span></div>`;
}

function renderSettingsWebPushSection() {
  const s = state.settings;
  // Codex chunk-10-step3a-close (1): use strict !== true so a null
  // value (pre-probe) doesn't fall through to the supported branch
  // and lie about the browser.
  if (s.pushApiAvailable !== true) {
    return `
      <section class="settings-section">
        <h3>Web Push</h3>
        ${_renderSettingsLine('Push API', '<span class="settings-pill badge-default">not supported</span>')}
        <p class="muted">This browser doesn't support Web Push. Install OpsMemory as a PWA in a supported browser (Chrome, Edge, Firefox, Safari 16.4+) to receive push notifications.</p>
      </section>
    `;
  }
  const vapidStatus = s.vapidPublicKey
    ? `<span class="settings-pill badge-granted">configured</span> <code class="settings-keyfrag">${escapeHtml(_shortenKey(s.vapidPublicKey))}</code>`
    : `<span class="settings-pill badge-default">not configured</span>`;
  const swStatus = s.serviceWorkerReady
    ? '<span class="settings-pill badge-granted">ready</span>'
    : '<span class="settings-pill badge-default">not ready</span>';
  const browserSubStatus = s.browserSubscription
    ? '<span class="settings-pill badge-granted">subscribed</span>'
    : '<span class="settings-pill badge-default">not subscribed</span>';

  const revokes = s.subscriptionRevokes || {};
  const tests = s.subscriptionTests || {};
  const subRows = (s.subscriptions || []).map(sub => {
    const pending = !!revokes[sub.id];
    const test = tests[sub.id];
    const testPending = !!(test && test.pending);
    let testPill = '';
    if (test && !test.pending) {
      if (test.status === 'sent') {
        testPill = ` <span class="settings-pill badge-granted" title="HTTP ${test.http || '?'} ${test.code || ''}">test sent</span>`;
      } else if (test.status === 'failed') {
        const label = test.code || 'failed';
        testPill = ` <span class="settings-pill badge-denied" title="HTTP ${test.http || '?'} ${test.detail || ''}">test ${escapeHtml(label)}</span>`;
      } else if (test.status === 'error') {
        testPill = ` <span class="settings-pill badge-denied" title="${escapeHtml(test.detail || 'error')}">test error</span>`;
      }
    }
    // Codex chunk-10-step5c2-close COMMIT 4 PLAN: per-row pending
    // disables the Test button. Revoke + Test share the row's
    // actions cell. Inline result pill (no toast).
    return `
    <tr>
      <td>${escapeHtml(sub.device_label || '(unlabeled device)')}</td>
      <td><code class="settings-keyfrag" title="${escapeHtml(sub.endpoint || '')}">${escapeHtml(_shortenKey(sub.endpoint))}</code></td>
      <td><span class="settings-pill badge-${sub.status === 'active' ? 'granted' : 'default'}">${escapeHtml(sub.status)}</span>${testPill}</td>
      <td class="muted">${escapeHtml(sub.last_seen_at || sub.created_at || '')}</td>
      <td class="settings-actions-cell">
        <button class="settings-test-push" data-subscription-id="${escapeHtml(sub.id)}"
                ${(testPending || pending || sub.status !== 'active') ? 'disabled' : ''}>${testPending ? 'Sending…' : 'Send test'}</button>
        <button class="settings-revoke" data-subscription-id="${escapeHtml(sub.id)}"
                ${pending ? 'disabled' : ''}>${pending ? 'Revoking…' : 'Revoke'}</button>
      </td>
    </tr>
    `;
  }).join('');
  // Codex chunk-10-step3a-close (i): table caption + scope="col"
  // for screen-reader navigation. Codex (e): show full endpoint in
  // the cell title for a hover tooltip — first 8 chars of an FCM URL
  // is otherwise useless.
  const subsTable = s.subscriptions && s.subscriptions.length
    ? `<table class="settings-table">
         <caption class="settings-table-caption">Active web push subscriptions for your account</caption>
         <thead><tr>
           <th scope="col">Device</th>
           <th scope="col">Endpoint</th>
           <th scope="col">Status</th>
           <th scope="col">Last seen</th>
           <th scope="col">Actions</th>
         </tr></thead>
         <tbody>${subRows}</tbody>
       </table>`
    : '<p class="muted">No active subscriptions on file.</p>';
  const revokeErrorBlock = s.subscriptionRevokeError
    ? `<div class="settings-row-error">${escapeHtml(s.subscriptionRevokeError)}</div>`
    : '';

  // Codex chunk-10-step3b2-close sub-(b)/3 plan:
  //   Button gate: pushApiAvailable && vapidPublicKey
  //                && serviceWorkerReady
  //                && permissionState !== 'denied'
  //                && !subscriptionCreatePending
  //   Don't gate ONLY on !browserSubscription — also expose a
  //   'Reconnect' path when the local sub exists but no server
  //   row matches its endpoint (failed cleanup or server-side
  //   revoke could otherwise strand the browser).
  const localEndpoint = s.browserSubscription ? s.browserSubscription.endpoint : null;
  const serverHasLocal = !!(localEndpoint
    && (s.subscriptions || []).some(sub => sub.endpoint === localEndpoint));
  // Codex chunk-10-step3b3-close (2): drop !subscriptionCreate
  // Pending from canTrySubscribe so the button is rendered (and
  // disabled) during a pending operation, not removed. This keeps
  // 'Enabling…' / 'Reconnecting…' labels visible.
  const prereqsMet = (
    s.pushApiAvailable === true
    && !!s.vapidPublicKey
    && s.permissionState !== 'denied'
  );
  const canTrySubscribe = prereqsMet && s.serviceWorkerReady;
  let actionRow = '';
  if (s.permissionState === 'denied') {
    actionRow = `
      <div class="settings-row settings-row-hint">
        <span class="settings-label">Notifications</span>
        <span class="settings-value muted">
          Permission was denied. To re-enable, open your browser's
          site settings for this app and allow notifications, then
          reload the page.
        </span>
      </div>
    `;
  } else if (prereqsMet && !s.serviceWorkerReady) {
    // Codex chunk-10-step3b3-close (1): SW-not-ready stranded the
    // UI with no action and no error. Surface a 'Check again'
    // button so the user has a path forward.
    actionRow = `
      <div class="settings-row">
        <span class="settings-label">Service worker</span>
        <span class="settings-value">
          <button class="settings-sw-recheck"
                  ${s.loading ? 'disabled' : ''}>${s.loading ? 'Checking…' : 'Check again'}</button>
          <span class="muted" style="margin-left:10px;">
            The service worker isn't ready yet. Click to recheck.
          </span>
        </span>
      </div>
    `;
  } else if (canTrySubscribe && (!s.browserSubscription || !serverHasLocal)) {
    const label = s.browserSubscription && !serverHasLocal
      ? (s.subscriptionCreatePending ? 'Reconnecting…' : 'Reconnect this browser')
      : (s.subscriptionCreatePending ? 'Enabling…' : 'Enable Web Push on this browser');
    actionRow = `
      <div class="settings-row">
        <span class="settings-label">Action</span>
        <span class="settings-value">
          <button class="settings-enable-push"
                  ${s.subscriptionCreatePending ? 'disabled' : ''}>${escapeHtml(label)}</button>
        </span>
      </div>
    `;
  }
  const createErrorBlock = s.subscriptionCreateError
    ? `<div class="settings-row-error">${escapeHtml(s.subscriptionCreateError)}</div>`
    : '';
  return `
    <section class="settings-section">
      <h3>Web Push</h3>
      ${_renderSettingsLine('Browser Push API', '<span class="settings-pill badge-granted">supported</span>')}
      ${_renderSettingsLine('Notification permission', _renderPermissionPill(s.permissionState))}
      ${_renderSettingsLine('Service worker', swStatus)}
      ${_renderSettingsLine('Server VAPID key', vapidStatus)}
      ${_renderSettingsLine('This browser', browserSubStatus)}
      ${actionRow}
      ${createErrorBlock}
      <h4>Active subscriptions</h4>
      ${revokeErrorBlock}
      ${subsTable}
    </section>
  `;
}

function _renderPrefDrawer(channel, draft) {
  // Codex chunk-10-step3b1: inline drawer row with colspan, not
  // cramming controls into the existing table cells.
  const validationError = _validateSettingsDraft(draft);
  const saveDisabled = draft.pending || !draft.dirty || !!validationError;
  const weeklyOpts = _SETTINGS_WEEKDAYS.map(d =>
    `<option value="${d}"${draft.weekday === d ? ' selected' : ''}>${d}</option>`).join('');
  const kindOpts = _SETTINGS_KIND_OPTIONS.map(k =>
    `<option value="${k}"${draft.scheduleKind === k ? ' selected' : ''}>${k}</option>`).join('');
  const hourPad = String(draft.hour).padStart(2, '0');
  const minPad = String(draft.minute).padStart(2, '0');
  const errorBlock = draft.error
    ? `<div class="settings-row-error">${escapeHtml(draft.error)}</div>`
    : (validationError
        ? `<div class="settings-row-error">${escapeHtml(validationError)}</div>`
        : '');
  return `
    <tr class="settings-drawer" data-drawer-channel="${escapeHtml(channel)}">
      <!-- colspan must match the prefs table column count
           (Channel/Status/Schedule/Settings/Actions = 5).
           Update both if a column is added. -->
      <td colspan="5">
        <div class="settings-editor">
          <label class="settings-editor-field">
            <input type="checkbox" data-pref-field="enabled" data-channel="${escapeHtml(channel)}"
                   ${draft.enabled ? 'checked' : ''}> Enabled
          </label>
          <label class="settings-editor-field">
            Kind
            <select data-pref-field="scheduleKind" data-channel="${escapeHtml(channel)}">${kindOpts}</select>
          </label>
          <label class="settings-editor-field">
            Hour
            <input type="number" min="0" max="23" inputmode="numeric"
                   data-pref-field="hour" data-channel="${escapeHtml(channel)}"
                   value="${hourPad}">
          </label>
          <label class="settings-editor-field">
            Minute
            <input type="number" min="0" max="59" inputmode="numeric"
                   data-pref-field="minute" data-channel="${escapeHtml(channel)}"
                   value="${minPad}">
          </label>
          ${draft.scheduleKind === 'weekly' ? `
            <label class="settings-editor-field">
              Weekday
              <select data-pref-field="weekday" data-channel="${escapeHtml(channel)}">${weeklyOpts}</select>
            </label>
          ` : ''}
          <span class="settings-editor-field muted">Timezone: ${escapeHtml(draft.timezone)}</span>
          <div class="settings-editor-actions">
            <button class="settings-edit-save" data-channel="${escapeHtml(channel)}"
                    ${saveDisabled ? 'disabled' : ''}>${draft.pending ? 'Saving…' : 'Save'}</button>
            <button class="settings-edit-cancel" data-channel="${escapeHtml(channel)}"
                    ${draft.pending ? 'disabled' : ''}>Cancel</button>
          </div>
          ${errorBlock}
        </div>
      </td>
    </tr>
  `;
}

function renderSettingsPrefsSection() {
  const s = state.settings;
  const editing = s.editing || {};
  const rows = (s.prefs || []).map(p => {
    const channel = p.channel;
    const enabledPill = p.enabled
      ? '<span class="settings-pill badge-granted">enabled</span>'
      : '<span class="settings-pill badge-default">disabled</span>';
    const defaultPill = p.synthesized_default
      ? ' <span class="settings-pill badge-default" title="No saved row yet — defaults shown.">default not yet saved</span>'
      : '';
    const settingsKv = (p.settings && Object.keys(p.settings).length)
      ? `<code class="settings-kv">${escapeHtml(JSON.stringify(p.settings))}</code>`
      : '<span class="muted">—</span>';
    const isEditing = !!editing[channel];
    const editButton = isEditing
      ? '<span class="muted">editing…</span>'
      : `<button class="settings-edit-open" data-channel="${escapeHtml(channel)}">Edit</button>`;
    const baseRow = `
      <tr class="${p.synthesized_default ? 'settings-row-default' : ''}">
        <td><strong>${escapeHtml(channel)}</strong>${defaultPill}</td>
        <td>${enabledPill}</td>
        <td>${_renderScheduleSummary(p.schedule)}</td>
        <td>${settingsKv}</td>
        <td class="settings-actions-cell">${editButton}</td>
      </tr>
    `;
    const drawerRow = isEditing ? _renderPrefDrawer(channel, editing[channel]) : '';
    return baseRow + drawerRow;
  }).join('');
  const table = s.prefs && s.prefs.length
    ? `<table class="settings-table">
         <caption class="settings-table-caption">Notification preferences by channel</caption>
         <thead><tr>
           <th scope="col">Channel</th>
           <th scope="col">Status</th>
           <th scope="col">Schedule</th>
           <th scope="col">Settings</th>
           <th scope="col">Actions</th>
         </tr></thead>
         <tbody>${rows}</tbody>
       </table>`
    : '<p class="muted">No preferences loaded.</p>';
  return `
    <section class="settings-section">
      <h3>Notification preferences</h3>
      ${table}
      <p class="muted">Edit a row to change schedule or enable a channel. Settings (stale_days, include_completed) edits land in the next update.</p>
    </section>
  `;
}

function renderSettings() {
  const s = state.settings;
  if (!s.loaded && s.loading) {
    return '<div class="settings-loading muted">Loading settings…</div>';
  }
  const errorPill = s.loadError
    ? `<div class="settings-error">⚠ ${escapeHtml(s.loadError)}</div>`
    : '';
  return `
    <div class="settings-shell">
      ${errorPill}
      ${renderSettingsWebPushSection()}
      ${renderSettingsPrefsSection()}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

function attachEventHandlers() {
  // ----- view tabs -----
  document.querySelectorAll('.view-tab').forEach(btn => {
    btn.addEventListener('click', async () => {
      const v = btn.dataset.view;
      if (state.view === v) return;
      // Codex chunk-7-step4-ui2 blocker: clear SOP editor on view
      // change so a stale editing state can't pick up a different
      // SOP/version on save.
      if (state.sops.editing && state.sops.editing.dirty) {
        if (!window.confirm('Discard unsaved SOP template edits?')) return;
      }
      state.sops.editing = null;
      // Codex chunk-10-step3b1 plan-review (10): mirror the SOP
      // dirty-tab guard for Settings drafts. Block navigation while
      // a save is pending; prompt to discard otherwise.
      if (hasPendingSettingsDraft()) {
        window.alert('A notification preference save is in flight; wait for it to finish.');
        return;
      }
      if (hasDirtySettingsDraft()) {
        if (!window.confirm('Discard unsaved notification preference edits?')) return;
      }
      state.settings.editing = {};
      state.view = v;
      state.review.actionError = null;
      if (v === 'review') {
        await refreshReviewItems();
      } else if (v === 'sops') {
        state.sops.loadError = null;
        await refreshSops();
      } else if (v === 'settings') {
        state.settings.loadError = null;
        // Codex chunk-10-step3a-close (1): set loading=true BEFORE
        // the initial render so the loading-shell branch in
        // renderSettings() actually fires. Without this the click
        // briefly shows uninitialized "Web Push: not supported"
        // because pushApiAvailable is still null.
        state.settings.loading = true;
        render();
        await refreshSettings();
      } else {
        render();
      }
    });
  });

  // ----- SOPs view handlers -----
  document.querySelectorAll('.sop-status-tab').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (state.sops.editing && state.sops.editing.dirty) {
        if (!window.confirm('Discard unsaved SOP template edits?')) return;
      }
      state.sops.editing = null;
      state.sops.statusFilter = btn.dataset.sopStatus;
      state.sops.expandedId = null;
      state.sops.expandedDetail = null;
      state.sops.selectedVersionNo = null;
      state.sops.selectedVersionDetail = null;
      await refreshSops();
    });
  });
  const sopBizFilter = document.getElementById('sop-business-filter');
  if (sopBizFilter) {
    sopBizFilter.addEventListener('change', async () => {
      if (state.sops.editing && state.sops.editing.dirty) {
        if (!window.confirm('Discard unsaved SOP template edits?')) {
          // Revert the dropdown to the prior selection.
          sopBizFilter.value = state.sops.businessFilter;
          return;
        }
      }
      state.sops.editing = null;
      state.sops.businessFilter = sopBizFilter.value;
      state.sops.expandedId = null;
      state.sops.expandedDetail = null;
      state.sops.selectedVersionNo = null;
      state.sops.selectedVersionDetail = null;
      await refreshSops();
    });
  }
  document.querySelectorAll('.sop-item').forEach(li => {
    li.addEventListener('click', async (e) => {
      // Don't bubble through nested version clicks (handled below).
      if (e.target.closest('.sop-version')) return;
      if (e.target.closest('button')) return;
      const sopId = li.dataset.sopId;
      // Codex chunk-7-step4-ui2 blocker: switching SOPs while editing
      // would leave stale editing state pointed at the other SOP's
      // version-no. Clear with dirty-confirm.
      if (state.sops.editing
          && state.sops.editing.sopId !== sopId
          && state.sops.editing.dirty) {
        if (!window.confirm('Discard unsaved SOP template edits?')) return;
      }
      if (state.sops.editing && state.sops.editing.sopId !== sopId) {
        state.sops.editing = null;
      }
      if (state.sops.expandedId === sopId) {
        // Collapsing the currently-edited SOP also clears editing
        // (with dirty-confirm above already handled).
        if (state.sops.editing && state.sops.editing.dirty) {
          if (!window.confirm('Discard unsaved SOP template edits?')) return;
        }
        state.sops.editing = null;
        state.sops.expandedId = null;
        state.sops.expandedDetail = null;
        state.sops.selectedVersionNo = null;
        state.sops.selectedVersionDetail = null;
      } else {
        state.sops.expandedId = sopId;
        state.sops.expandedDetail = null;
        state.sops.selectedVersionNo = null;
        state.sops.selectedVersionDetail = null;
        state.sops.loadError = null;
        render();
        try {
          const detail = await loadSopDetail(sopId);
          if (state.sops.expandedId !== sopId) return;
          state.sops.expandedDetail = detail;
        } catch (err) {
          if (state.sops.expandedId !== sopId) return;
          state.sops.loadError = err.message || 'Failed to load SOP detail.';
        }
      }
      render();
    });
  });
  // ----- SOP authoring (UI 2/3) -----
  document.querySelectorAll('.sop-create-draft').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const sopId = btn.dataset.sopId;
      let newVersion;
      try {
        newVersion = await createSopDraftVersion(sopId, {});
      } catch (err) {
        state.sops.loadError = formatSopError('create draft', err);
        render();
        return;
      }
      // Draft is created server-side. Try to refresh the local detail
      // so the new row appears; on refresh failure, surface "draft
      // created; refresh failed locally" rather than swallowing the
      // create success (Codex chunk-7-step4-ui2 (h)).
      try {
        const detail = await loadSopDetail(sopId);
        state.sops.expandedDetail = detail;
      } catch {
        state.sops.loadError = 'Draft created; UI refresh failed. Reload to see it.';
        render();
        return;
      }
      state.sops.selectedVersionNo = newVersion.version_no;
      state.sops.selectedVersionDetail = { version: newVersion, template_tasks: [] };
      state.sops.editing = {
        sopId,
        versionNo: newVersion.version_no,
        templates: [],
        saving: false,
        saveError: null,
        publishError: null,
        pending: null,
        dirty: false,
      };
      render();
    });
  });

  document.querySelectorAll('.sop-version-edit').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const versionNo = parseInt(btn.dataset.versionNo, 10);
      const sopId = state.sops.expandedId;
      if (!sopId) return;
      // Codex chunk-7-step4-ui2 blocker: must load version detail
      // BEFORE entering edit mode if the loaded detail doesn't
      // match this (sopId, versionNo). Otherwise a fast click on
      // a slow network opens an empty editor over a real draft
      // and Save (replace-all PATCH) wipes the draft's templates.
      let detail = state.sops.selectedVersionDetail;
      const detailMatches = detail
        && state.sops.selectedVersionNo === versionNo;
      if (!detailMatches) {
        try {
          detail = await loadSopVersionDetail(sopId, versionNo);
          // Race: another click changed the selection while this
          // fetch was in flight.
          if (state.sops.expandedId !== sopId
              || state.sops.selectedVersionNo !== versionNo) {
            // Update detail anyway so the next render is consistent.
            return;
          }
          state.sops.selectedVersionDetail = detail;
        } catch (err) {
          state.sops.loadError = formatSopError('load draft', err);
          render();
          return;
        }
      }
      const baseline = (detail && detail.template_tasks)
        ? detail.template_tasks.map(t => ({
            summary: t.summary || '',
            description: t.description || '',
            due_offset_days: t.due_offset_days,
            dependency_text: t.dependency_text || '',
            category: t.category || '',
            priority: t.priority || '',
            owner_role: t.owner_role || '',
          }))
        : [];
      state.sops.editing = {
        sopId,
        versionNo,
        templates: baseline,
        saving: false,
        saveError: null,
        publishError: null,
        pending: null,
        dirty: false,
      };
      render();
    });
  });

  // Capture template-row input changes into state.sops.editing.templates.
  document.querySelectorAll('.sop-template-edit input, .sop-template-edit textarea').forEach(input => {
    input.addEventListener('input', () => {
      if (!state.sops.editing) return;
      const li = input.closest('.sop-template-edit');
      if (!li) return;
      const idx = parseInt(li.dataset.editIdx, 10);
      const field = input.dataset.field;
      if (!field || !state.sops.editing.templates[idx]) return;
      let value = input.value;
      if (field === 'due_offset_days') {
        value = (value === '') ? null : parseInt(value, 10);
        if (Number.isNaN(value)) value = null;
      } else if (value === '') {
        value = null;  // empty string -> null for optional fields
      }
      state.sops.editing.templates[idx][field] = value;
      state.sops.editing.dirty = true;
      // No re-render: input keeps focus.
    });
  });

  document.querySelectorAll('.t-remove').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!state.sops.editing) return;
      const idx = parseInt(btn.dataset.editIdx, 10);
      state.sops.editing.templates.splice(idx, 1);
      state.sops.editing.dirty = true;
      render();
    });
  });

  document.querySelectorAll('.sop-edit-add-row').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!state.sops.editing) return;
      state.sops.editing.templates.push({
        summary: '',
        description: '',
        due_offset_days: null,
        dependency_text: '',
        category: '',
        priority: '',
        owner_role: '',
      });
      state.sops.editing.dirty = true;
      render();
    });
  });

  document.querySelectorAll('.sop-edit-cancel').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (state.sops.editing && state.sops.editing.dirty) {
        if (!window.confirm('Discard unsaved template edits?')) return;
      }
      state.sops.editing = null;
      render();
    });
  });

  document.querySelectorAll('.sop-edit-save').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      await _saveSopTemplatesFlow();
    });
  });

  document.querySelectorAll('.sop-edit-publish').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!state.sops.editing) return;
      const versionNo = state.sops.editing.versionNo;
      const ok = window.confirm(
        `Publish v${versionNo}? Any prior published version will be superseded. Templates become immutable after publish.`
      );
      if (!ok) return;
      // Codex chunk-7-step4-ui2 blocker: Publish must consume any
      // unsaved editor changes first; otherwise the OLD draft state
      // is published and frozen, silently losing the edits.
      if (state.sops.editing.dirty) {
        const saved = await _saveSopTemplatesFlow();
        if (!saved) return;  // save failed; surface error and stop
      }
      const changeLog = window.prompt('Change log (optional):', '') || '';
      const ed = state.sops.editing;
      if (!ed) return;
      const sopId = ed.sopId;
      ed.pending = 'publish';
      ed.publishError = null;
      render();
      try {
        await publishSopVersion(sopId, versionNo,
                                 changeLog.trim() ? { change_log: changeLog.trim() } : {});
      } catch (err) {
        ed.pending = null;
        ed.publishError = formatSopError('publish', err);
        render();
        return;
      }
      // Publish succeeded server-side. Clear editing first so a
      // subsequent refresh failure doesn't leave a misleading editor
      // open (Codex chunk-7-step4-ui2 (i)).
      state.sops.editing = null;
      try {
        const detail = await loadSopDetail(sopId);
        state.sops.expandedDetail = detail;
        state.sops.selectedVersionDetail = await loadSopVersionDetail(sopId, versionNo);
        await refreshSops();
      } catch {
        state.sops.loadError = 'Published; UI refresh failed. Reload to see latest state.';
      }
      render();
    });
  });

  document.querySelectorAll('.sop-version').forEach(li => {
    li.addEventListener('click', async (e) => {
      e.stopPropagation();
      // Don't toggle the version on button / input / textarea clicks
      // (the editor lives inside .sop-version when editing).
      if (e.target.closest('button')) return;
      if (e.target.closest('input, textarea, select')) return;
      if (e.target.closest('.sop-edit-pane')) return;
      const versionNo = parseInt(li.dataset.versionNo, 10);
      const sopId = state.sops.expandedId;
      if (!sopId) return;
      if (state.sops.selectedVersionNo === versionNo) {
        state.sops.selectedVersionNo = null;
        state.sops.selectedVersionDetail = null;
      } else {
        state.sops.selectedVersionNo = versionNo;
        state.sops.selectedVersionDetail = null;
        // Capture the SOP id under which this fetch was issued so a
        // subsequent collapse-then-expand of a different SOP doesn't
        // commit this fetch's result under the new SOP. Codex flagged
        // that the version-no race guard alone doesn't cover the
        // (sop_id, version_no) tuple — version_no=1 exists on every
        // SOP.
        const fetchSopId = sopId;
        render();
        try {
          const detail = await loadSopVersionDetail(sopId, versionNo);
          if (state.sops.expandedId !== fetchSopId
              || state.sops.selectedVersionNo !== versionNo) return;
          state.sops.selectedVersionDetail = detail;
        } catch (err) {
          if (state.sops.expandedId !== fetchSopId
              || state.sops.selectedVersionNo !== versionNo) return;
          state.sops.loadError = err.message || 'Failed to load version templates.';
        }
      }
      render();
    });
  });

  // ----- review handlers (only present when view='review') -----
  document.querySelectorAll('.rv-item').forEach(li => {
    li.addEventListener('click', async (e) => {
      // Buttons inside the item handle their own clicks below.
      if (e.target.closest('button') || e.target.closest('details')) return;
      const rid = li.dataset.reviewId;
      if (state.review.expandedId === rid) {
        state.review.expandedId = null;
        state.review.expandedDetail = null;
      } else {
        state.review.expandedId = rid;
        state.review.expandedDetail = null;
        state.review.actionError = null;
        render();
        try {
          const detail = await loadReviewDetail(rid);
          if (state.review.expandedId !== rid) return;
          state.review.expandedDetail = detail;
        } catch (err) {
          if (state.review.expandedId !== rid) return;
          state.review.actionError = err.message || 'Failed to load detail.';
        }
      }
      render();
    });
  });

  document.querySelectorAll('.rv-approve').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (state.review.pendingAction) return;
      const rid = btn.dataset.rid;
      state.review.pendingAction = 'approve';
      state.review.actionError = null;
      render();
      try {
        await approveReview(rid);
        state.review.expandedId = null;
        state.review.expandedDetail = null;
        await refreshReviewItems();
      } catch (err) {
        state.review.actionError = formatActionError('approve', err);
        // Refresh to pick up the demoted status + last_apply_error.
        await refreshReviewItems();
        if (state.review.expandedId === rid) {
          try {
            state.review.expandedDetail = await loadReviewDetail(rid);
          } catch { /* swallow */ }
        }
      } finally {
        state.review.pendingAction = null;
        render();
      }
    });
  });

  document.querySelectorAll('.rv-reject').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (state.review.pendingAction) return;
      const rid = btn.dataset.rid;
      const reason = window.prompt('Reject reason (optional):', '') || '';
      state.review.pendingAction = 'reject';
      state.review.actionError = null;
      render();
      try {
        await rejectReview(rid, reason.trim() || null);
        state.review.expandedId = null;
        state.review.expandedDetail = null;
        await refreshReviewItems();
      } catch (err) {
        state.review.actionError = formatActionError('reject', err);
      } finally {
        state.review.pendingAction = null;
        render();
      }
    });
  });

  // ----- tasks-view handlers -----
  document.querySelectorAll('.status-tabs .tab').forEach(btn => {
    btn.addEventListener('click', async () => {
      state.filters.status = btn.dataset.status;
      state.expandedTaskId = null;
      state.expandedTaskDetail = null;
      await refreshTasks();
    });
  });

  const bizFilter = document.getElementById('business-filter');
  if (bizFilter) {
    bizFilter.addEventListener('change', async () => {
      state.filters.business = bizFilter.value;
      state.expandedTaskId = null;
      state.expandedTaskDetail = null;
      await refreshTasks();
    });
  }

  document.querySelectorAll('.task').forEach(li => {
    li.addEventListener('click', async (e) => {
      // Don't toggle when clicking on inner links/buttons in detail.
      if (e.target.closest('a')) return;
      if (e.target.closest('button')) return;
      const taskId = li.dataset.taskId;
      if (state.expandedTaskId === taskId) {
        state.expandedTaskId = null;
        state.expandedTaskDetail = null;
      } else {
        state.expandedTaskId = taskId;
        state.expandedTaskDetail = null;
        render();  // immediate re-render with loading state
        try {
          const detail = await loadTaskDetail(taskId);
          // Race guard: if the user clicked a different task while this
          // fetch was in flight, don't render stale detail under the new
          // selection. Codex chunk-2-close: was rendering A's detail
          // under B if A's request finished after B was clicked.
          if (state.expandedTaskId !== taskId) return;
          state.expandedTaskDetail = detail;
        } catch (err) {
          if (state.expandedTaskId !== taskId) return;
          renderError(err);
          return;
        }
      }
      render();
    });
  });

  // ----- Done / Reopen buttons (chunk 6 step 3) -----
  document.querySelectorAll('.task-complete, .task-reopen').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const taskId = btn.dataset.taskId;
      const local = getTaskFromState(taskId);
      if (!local) return;
      try {
        await enqueueToggleDone(local);
      } catch (err) {
        renderError(err);
      }
    });
  });

  // ----- Conflict retry / discard -----
  document.querySelectorAll('.conflict-retry').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      await retryConflict(btn.dataset.key);
    });
  });
  document.querySelectorAll('.conflict-discard').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      await discardConflict(btn.dataset.key);
    });
  });

  // ===== UI 3/3: Create SOP / Anchors / Schedule / Fire =====

  // ---- Create SOP form ----
  document.querySelectorAll('.sop-create-open').forEach(btn => {
    btn.addEventListener('click', () => {
      state.sops.creating = {
        businessSlug: '',
        name: '',
        description: '',
        pending: false,
        error: null,
      };
      render();
    });
  });
  document.querySelectorAll('.sop-create-cancel').forEach(btn => {
    btn.addEventListener('click', () => {
      state.sops.creating = null;
      render();
    });
  });
  const sopCreateBiz = document.getElementById('sop-create-biz');
  if (sopCreateBiz) {
    sopCreateBiz.addEventListener('change', () => {
      if (state.sops.creating) state.sops.creating.businessSlug = sopCreateBiz.value;
    });
  }
  const sopCreateName = document.getElementById('sop-create-name');
  if (sopCreateName) {
    sopCreateName.addEventListener('input', () => {
      if (state.sops.creating) state.sops.creating.name = sopCreateName.value;
    });
  }
  const sopCreateDesc = document.getElementById('sop-create-desc');
  if (sopCreateDesc) {
    sopCreateDesc.addEventListener('input', () => {
      if (state.sops.creating) state.sops.creating.description = sopCreateDesc.value;
    });
  }
  document.querySelectorAll('.sop-create-submit').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!state.sops.creating) return;
      const c = state.sops.creating;
      if (!c.businessSlug || !c.name.trim()) {
        c.error = 'Business and name are required.';
        render();
        return;
      }
      c.pending = true;
      c.error = null;
      render();
      try {
        const created = await createSop({
          business_slug: c.businessSlug,
          name: c.name.trim(),
          description: c.description.trim() || null,
        });
        state.sops.creating = null;
        await refreshSops();
        // Auto-expand the new SOP so the user can immediately
        // create a draft version.
        state.sops.expandedId = created.id;
        try {
          state.sops.expandedDetail = await loadSopDetail(created.id);
        } catch { /* fine — empty detail; user can click again */ }
        render();
      } catch (err) {
        c.pending = false;
        c.error = formatSopError('create SOP', err);
        render();
      }
    });
  });

  // ---- Anchors section toggle + filters ----
  document.querySelectorAll('.anchors-open').forEach(btn => {
    btn.addEventListener('click', async () => {
      state.anchors.expanded = true;
      await refreshAnchors();
    });
  });
  document.querySelectorAll('.anchors-close').forEach(btn => {
    btn.addEventListener('click', () => {
      state.anchors.expanded = false;
      state.anchors.scheduling = null;
      state.anchors.fireResult = null;
      render();
    });
  });
  document.querySelectorAll('.anchor-status-tab').forEach(btn => {
    btn.addEventListener('click', async () => {
      state.anchors.statusFilter = btn.dataset.anchorStatus;
      await refreshAnchors();
    });
  });
  const anchorBizFilter = document.getElementById('anchor-business-filter');
  if (anchorBizFilter) {
    anchorBizFilter.addEventListener('change', async () => {
      state.anchors.businessFilter = anchorBizFilter.value;
      await refreshAnchors();
    });
  }

  // ---- Schedule anchor form ----
  document.querySelectorAll('.anchor-schedule-open').forEach(btn => {
    btn.addEventListener('click', () => {
      state.anchors.scheduling = {
        businessSlug: '',
        sopId: '',
        kind: '',
        scheduledForLocal: '',
        notes: '',
        sopOptions: [],
        pending: false,
        error: null,
      };
      render();
    });
  });
  document.querySelectorAll('.anchor-sched-cancel').forEach(btn => {
    btn.addEventListener('click', () => {
      state.anchors.scheduling = null;
      render();
    });
  });
  const anchorSchedBiz = document.getElementById('anchor-sched-biz');
  if (anchorSchedBiz) {
    anchorSchedBiz.addEventListener('change', async () => {
      if (!state.anchors.scheduling) return;
      const slug = anchorSchedBiz.value;
      state.anchors.scheduling.businessSlug = slug;
      state.anchors.scheduling.sopId = '';
      state.anchors.scheduling.sopOptions = [];
      if (slug) {
        try {
          const sops = await loadActiveSopsForBusiness(slug);
          if (state.anchors.scheduling
              && state.anchors.scheduling.businessSlug === slug) {
            state.anchors.scheduling.sopOptions = sops;
          }
        } catch (err) {
          if (state.anchors.scheduling) {
            state.anchors.scheduling.error = formatSopError('load SOPs for business', err);
          }
        }
      }
      render();
    });
  }
  const anchorSchedSop = document.getElementById('anchor-sched-sop');
  if (anchorSchedSop) {
    anchorSchedSop.addEventListener('change', () => {
      if (state.anchors.scheduling) state.anchors.scheduling.sopId = anchorSchedSop.value;
    });
  }
  const anchorSchedKind = document.getElementById('anchor-sched-kind');
  if (anchorSchedKind) {
    anchorSchedKind.addEventListener('input', () => {
      if (state.anchors.scheduling) state.anchors.scheduling.kind = anchorSchedKind.value;
    });
  }
  const anchorSchedWhen = document.getElementById('anchor-sched-when');
  if (anchorSchedWhen) {
    anchorSchedWhen.addEventListener('input', () => {
      if (state.anchors.scheduling) state.anchors.scheduling.scheduledForLocal = anchorSchedWhen.value;
    });
  }
  const anchorSchedNotes = document.getElementById('anchor-sched-notes');
  if (anchorSchedNotes) {
    anchorSchedNotes.addEventListener('input', () => {
      if (state.anchors.scheduling) state.anchors.scheduling.notes = anchorSchedNotes.value;
    });
  }
  document.querySelectorAll('.anchor-sched-submit').forEach(btn => {
    btn.addEventListener('click', async () => {
      const s = state.anchors.scheduling;
      if (!s) return;
      if (!s.businessSlug || !s.sopId || !s.kind.trim() || !s.scheduledForLocal) {
        s.error = 'Business, SOP, kind, and scheduled time are required.';
        render();
        return;
      }
      // datetime-local has no timezone. The server requires a tz-aware
      // ISO string (per _parse_iso_timestamp). Convert via Date so the
      // browser's local timezone is the operator's intent, then send
      // the equivalent ISO with offset.
      let scheduledIso;
      try {
        const d = new Date(s.scheduledForLocal);
        if (Number.isNaN(d.getTime())) throw new Error('bad date');
        scheduledIso = d.toISOString();  // always Z (UTC)
      } catch {
        s.error = 'Invalid scheduled date/time.';
        render();
        return;
      }
      s.pending = true;
      s.error = null;
      render();
      try {
        await createAnchor({
          business_slug: s.businessSlug,
          sop_id: s.sopId,
          kind: s.kind.trim(),
          scheduled_for: scheduledIso,
          notes: s.notes.trim() || null,
        });
        state.anchors.scheduling = null;
        await refreshAnchors();
      } catch (err) {
        s.pending = false;
        s.error = formatSopError('schedule anchor', err);
        render();
      }
    });
  });

  // ---- Fire anchor ----
  document.querySelectorAll('.anchor-fire').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const anchorId = btn.dataset.anchorId;
      if (!window.confirm('Fire this anchor? This materializes review items for every template — irreversible.')) return;
      try {
        const result = await fireAnchor(anchorId);
        state.anchors.fireResult = {
          anchorId: result.anchor_event_id,
          reviewItemsCreated: result.review_items_created,
          instanceId: result.sop_instance_id,
        };
        await refreshAnchors();
      } catch (err) {
        state.anchors.loadError = formatSopError('fire anchor', err);
        render();
      }
    });
  });

  // ---- Go to Review (post-fire) ----
  document.querySelectorAll('.goto-review').forEach(btn => {
    btn.addEventListener('click', async () => {
      state.view = 'review';
      state.review.actionError = null;
      state.anchors.fireResult = null;
      await refreshReviewItems();
    });
  });

  // ---- Settings: prefs editor (chunk 10 step 3 sub-(b) commit 1) ----
  document.querySelectorAll('.settings-edit-open').forEach(btn => {
    btn.addEventListener('click', () => {
      const channel = btn.dataset.channel;
      if (!channel) return;
      const pref = (state.settings.prefs || []).find(p => p.channel === channel);
      if (!pref) return;
      // Codex chunk-10-step3b1 (5): unknown kind => render read-only
      // (don't seed an editable draft from a kind we can't represent).
      const kind = (pref.schedule || {}).kind;
      if (kind && !_SETTINGS_KIND_OPTIONS.includes(kind)) {
        window.alert(`Schedule kind "${kind}" is not supported by this editor.`);
        return;
      }
      state.settings.editing = state.settings.editing || {};
      state.settings.editing[channel] = _seedSettingsDraft(pref);
      render();
    });
  });

  document.querySelectorAll('.settings-edit-cancel').forEach(btn => {
    btn.addEventListener('click', () => {
      const channel = btn.dataset.channel;
      if (!channel) return;
      const draft = (state.settings.editing || {})[channel];
      if (draft && draft.pending) return;
      delete state.settings.editing[channel];
      render();
    });
  });

  // Field-change listeners for the drawer.
  //
  // Codex chunk-10-step3b1-close (blocker 1): re-rendering on every
  // 'input' event for number fields destroys/recreates the focused
  // element and can eat keystrokes / Save clicks. Mirror the SOP
  // template editor pattern (web/app.js earlier in this same
  // function): mutate state on 'input', do NOT render. Number /
  // text 'input' events update the draft in-memory and toggle the
  // Save button's disabled attribute directly via DOM. Only the
  // checkbox + select fire 'change' (and only the kind select
  // requires a re-render to show/hide the weekday dropdown).
  function _refreshSaveButtonState(channel, draft) {
    const saveBtn = document.querySelector(
      `.settings-edit-save[data-channel="${CSS.escape(channel)}"]`
    );
    if (!saveBtn) return;
    const validationErr = _validateSettingsDraft(draft);
    saveBtn.disabled = draft.pending || !draft.dirty || !!validationErr;
  }
  document.querySelectorAll('input[data-pref-field][type="number"]').forEach(el => {
    el.addEventListener('input', () => {
      const channel = el.dataset.channel;
      const field = el.dataset.prefField;
      const draft = (state.settings.editing || {})[channel];
      if (!draft || draft.pending) return;
      const n = parseInt(el.value, 10);
      const next = Number.isFinite(n) ? n : NaN;
      if (draft[field] !== next) {
        draft[field] = next;
        draft.dirty = true;
        draft.error = null;
        _refreshSaveButtonState(channel, draft);
      }
    });
  });
  document.querySelectorAll('input[data-pref-field][type="checkbox"], select[data-pref-field]').forEach(el => {
    el.addEventListener('change', () => {
      const channel = el.dataset.channel;
      const field = el.dataset.prefField;
      const draft = (state.settings.editing || {})[channel];
      if (!draft || draft.pending) return;
      let next;
      if (field === 'enabled') next = !!el.checked;
      else next = el.value;
      if (draft[field] !== next) {
        draft[field] = next;
        draft.dirty = true;
        draft.error = null;
        // scheduleKind toggles the visibility of the weekday <select>,
        // so a re-render is required when kind changes. enabled
        // checkbox + weekday select don't change layout, but
        // re-rendering them is cheap and they don't suffer the
        // focus-eating issue (change fires on blur for selects).
        render();
      }
    });
  });

  // Codex chunk-10-step3b3-close (1): manual SW recheck.
  document.querySelectorAll('.settings-sw-recheck').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (state.settings.loading) return;
      state.settings.loading = true;
      render();
      try {
        const probe = await _readServiceWorkerSubscription(2000);
        state.settings.serviceWorkerReady = !!probe.ready;
        state.settings.browserSubscription = probe.subscription || null;
      } catch (err) {
        // No-op: probe already swallows; UI stays in not-ready state.
      } finally {
        state.settings.loading = false;
        render();
      }
    });
  });

  // Codex chunk-10-step3b2-close sub-(b)/3: Enable Web Push +
  // Reconnect this browser. Single button; the renderer picks
  // the label based on whether browserSubscription matches a
  // server row. The handler does:
  //   - guard pending
  //   - request permission if not granted
  //   - get SW registration
  //   - subscribe (or reuse existing browserSubscription for
  //     reconnect) -> extract keys -> POST to server
  //   - on POST failure after subscribe: best-effort unsubscribe
  //   - on success: refetch subscriptions + permission +
  //     browserSubscription
  document.querySelectorAll('.settings-enable-push').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (state.settings.subscriptionCreatePending) return;
      state.settings.subscriptionCreatePending = true;
      state.settings.subscriptionCreateError = null;
      render();
      let createdSubscription = null;
      let weCreatedIt = false;
      try {
        // Step 2: request permission if needed. Must be in
        // direct response to a user gesture (we are: this is
        // the click handler itself).
        if (state.settings.permissionState !== 'granted') {
          if (!('Notification' in window)) {
            throw new Error('Notification API unavailable.');
          }
          const result = await window.Notification.requestPermission();
          state.settings.permissionState = result;
          if (result !== 'granted') {
            throw new Error('Notification permission was not granted.');
          }
        }
        // Step 4: get SW registration.
        const reg = await navigator.serviceWorker.ready;
        if (!reg || !reg.pushManager) {
          throw new Error('Service worker / Push manager unavailable.');
        }
        // Step 5: reuse existing subscription if present, else
        // call .subscribe() with the VAPID key.
        let sub = await reg.pushManager.getSubscription();
        if (!sub) {
          if (!state.settings.vapidPublicKey) {
            throw new Error('Server VAPID key missing; ask admin.');
          }
          sub = await reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(state.settings.vapidPublicKey),
          });
          weCreatedIt = true;
        }
        createdSubscription = sub;
        // Step 6+7: extract keys and POST.
        const keys = _extractSubscriptionKeys(sub);
        if (!keys.p256dh || !keys.auth) {
          throw new Error('Subscription keys unavailable.');
        }
        const ua = (typeof navigator !== 'undefined' && navigator.userAgent) || '';
        const body = {
          endpoint: sub.endpoint,
          p256dh_key: keys.p256dh,
          auth_key: keys.auth,
          device_label: null,
          user_agent: ua.length > 512 ? ua.slice(0, 512) : ua,
        };
        const postResponse = await postPushSubscription(body);
        // Step 9: refetch subscriptions + re-probe SW state.
        try {
          const subsResp = await loadPushSubscriptions();
          state.settings.subscriptions = (subsResp && subsResp.items) || [];
        } catch (refetchErr) {
          // Codex chunk-10-step3b3-close (10): if refetch fails
          // the local list stays stale and serverHasLocal would
          // flip the UI back to 'Reconnect this browser'
          // immediately after a successful POST. Merge the POST
          // response into local state so serverHasLocal stays
          // true.
          if (postResponse && postResponse.id && postResponse.endpoint) {
            const existing = (state.settings.subscriptions || []).filter(
              x => x.endpoint !== postResponse.endpoint
            );
            state.settings.subscriptions = [postResponse, ...existing];
          }
          state.settings.subscriptionCreateError =
            'Enabled. The device list did not refresh; reload to verify.';
        }
        try {
          const probe = await _readServiceWorkerSubscription(2000);
          state.settings.serviceWorkerReady = !!probe.ready;
          state.settings.browserSubscription = probe.subscription || sub;
        } catch (probeErr) {
          state.settings.browserSubscription = sub;
        }
      } catch (err) {
        // If we created the browser subscription but the server
        // POST or downstream step failed, best-effort
        // unsubscribe so the browser doesn't stay subscribed
        // without a server record.
        if (weCreatedIt && createdSubscription) {
          try { await createdSubscription.unsubscribe(); }
          catch (unsubErr) { console.warn('[opsmemory] cleanup unsubscribe() failed', unsubErr); }
        }
        let msg = (err && err.message) || 'Could not enable Web Push.';
        const detail = err && err.body && err.body.detail;
        if (typeof detail === 'string') msg = detail;
        else if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
          msg = detail.reason || detail.detail || detail.code || msg;
        } else if (Array.isArray(detail) && detail.length && detail[0].msg) {
          msg = detail[0].msg;
        }
        state.settings.subscriptionCreateError = msg;
        // Re-probe permission state so UI reflects user's choice
        // if they denied the prompt.
        state.settings.permissionState = readNotificationPermission();
      } finally {
        state.settings.subscriptionCreatePending = false;
        render();
      }
    });
  });

  // Codex chunk-10-step5c2-close COMMIT 4 PLAN: Send test push.
  // Per-row pending state in state.settings.subscriptionTests.
  // Inline pill on result; no toast. Errors don't disturb the
  // global revoke error pill.
  document.querySelectorAll('.settings-test-push').forEach(btn => {
    btn.addEventListener('click', async () => {
      const subId = btn.dataset.subscriptionId;
      if (!subId) return;
      const tests = state.settings.subscriptionTests || {};
      if (tests[subId] && tests[subId].pending) return;
      state.settings.subscriptionTests = {
        ...tests,
        [subId]: { pending: true },
      };
      render();
      let next = { pending: false };
      try {
        const resp = await postTestPush(subId);
        next = {
          pending: false,
          status: resp.status || 'failed',
          http: resp.http_status,
          code: resp.code,
          detail: resp.detail,
        };
      } catch (err) {
        let detail = (err && err.message) || 'Test failed.';
        const errDetail = err && err.body && err.body.detail;
        if (typeof errDetail === 'string') detail = errDetail;
        else if (errDetail && typeof errDetail === 'object' && !Array.isArray(errDetail)) {
          detail = errDetail.reason || errDetail.detail || errDetail.code || detail;
        }
        next = {
          pending: false,
          status: 'error',
          http: (err && err.status) || null,
          code: (errDetail && errDetail.code) || null,
          detail,
        };
      }
      state.settings.subscriptionTests = {
        ...(state.settings.subscriptionTests || {}),
        [subId]: next,
      };
      render();
    });
  });

  // Codex chunk-10-step3b2: per-device Revoke flow. Per-row
  // pending map keys on subscription id; window.confirm gates
  // the destructive action; matches the SOP "Discard edits?"
  // pattern. Saved state in subscriptionRevokes is consulted by
  // both the renderer (button label/disabled) and the click
  // handler (idempotent guard).
  document.querySelectorAll('.settings-revoke').forEach(btn => {
    btn.addEventListener('click', async () => {
      const subId = btn.dataset.subscriptionId;
      if (!subId) return;
      // Find the row before any state mutation so we can match
      // local browserSubscription endpoint after server success.
      const sub = (state.settings.subscriptions || []).find(s => s.id === subId);
      if (!sub) return;
      const revokes = state.settings.subscriptionRevokes || {};
      if (revokes[subId]) return;  // already in flight
      if (!window.confirm('Revoke this device? It will stop receiving push notifications.')) return;
      // Codex chunk-10-step3b2-close (a): re-read live state after
      // confirm, not the captured-before-confirm snapshot. The
      // earlier copy in `revokes` was a closure capture and would
      // miss any intervening pending update.
      if ((state.settings.subscriptionRevokes || {})[subId]) return;

      const localEndpoint = state.settings.browserSubscription
        ? state.settings.browserSubscription.endpoint
        : null;
      const isLocalDevice = !!(sub.endpoint && localEndpoint && sub.endpoint === localEndpoint);

      state.settings.subscriptionRevokes = { ...revokes, [subId]: true };
      state.settings.subscriptionRevokeError = null;
      render();

      let serverOk = false;
      try {
        await deletePushSubscription(subId);
        serverOk = true;
      } catch (err) {
        // Codex chunk-10-step3b2 plan-review (c): server-DELETE
        // failure shows the error pill and leaves the row.
        let msg = (err && err.message) || 'Revoke failed.';
        const detail = err && err.body && err.body.detail;
        if (typeof detail === 'string') msg = detail;
        else if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
          msg = detail.reason || detail.detail || detail.code || msg;
        }
        state.settings.subscriptionRevokeError = msg;
      }

      if (serverOk) {
        // Codex chunk-10-step3b2 (c): refetch is the primary path
        // to keep multi-tab state honest. If refetch FAILS, splice
        // out the revoked row locally — but DON'T phrase that as
        // a delete failure, since the server delete succeeded.
        try {
          const subsResp = await loadPushSubscriptions();
          state.settings.subscriptions = (subsResp && subsResp.items) || [];
        } catch (refetchErr) {
          state.settings.subscriptions = (state.settings.subscriptions || [])
            .filter(s => s.id !== subId);
          state.settings.subscriptionRevokeError =
            'Revoked, but the device list failed to refresh. Reload the page to verify.';
        }
        // Best-effort local unsubscribe + SW state re-probe.
        if (isLocalDevice) {
          try {
            if (state.settings.browserSubscription
                && typeof state.settings.browserSubscription.unsubscribe === 'function') {
              await state.settings.browserSubscription.unsubscribe();
            }
          } catch (unsubErr) {
            console.warn('[opsmemory] local unsubscribe() failed', unsubErr);
          }
          try {
            const probe = await _readServiceWorkerSubscription(2000);
            state.settings.serviceWorkerReady = !!probe.ready;
            state.settings.browserSubscription = probe.subscription || null;
          } catch (probeErr) {
            state.settings.browserSubscription = null;
          }
        }
      }

      // Always clear the per-row pending flag.
      const after = { ...state.settings.subscriptionRevokes };
      delete after[subId];
      state.settings.subscriptionRevokes = after;
      render();
    });
  });

  document.querySelectorAll('.settings-edit-save').forEach(btn => {
    btn.addEventListener('click', async () => {
      const channel = btn.dataset.channel;
      if (!channel) return;
      const draft = (state.settings.editing || {})[channel];
      if (!draft) return;
      // Codex chunk-10-step3b1 plan-review: save-handler guard is
      // separate from the disabled attribute. Disabled buttons can
      // still fire on rapid double-click in some browsers.
      if (draft.pending) return;
      const validationErr = _validateSettingsDraft(draft);
      if (validationErr) {
        draft.error = validationErr;
        render();
        return;
      }
      draft.pending = true;
      draft.error = null;
      render();
      try {
        const body = _serializeSettingsDraft(draft);
        const updated = await patchNotificationPref(channel, body);
        // Replace the row in state.settings.prefs with the saved
        // version (so synthesized_default badge clears).
        state.settings.prefs = (state.settings.prefs || []).map(p =>
          p.channel === channel ? updated : p
        );
        delete state.settings.editing[channel];
      } catch (err) {
        draft.pending = false;
        // Codex chunk-10-step3b1-close (f): user-friendly message
        // extraction. Pydantic 422 detail is a list of {type, loc,
        // msg, ...} objects; show the first msg. Our hand-rolled
        // 422s use {code, reason}.
        let msg = (err && err.message) || 'Save failed.';
        const detail = err && err.body && err.body.detail;
        if (typeof detail === 'string') {
          msg = detail;
        } else if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
          msg = detail.reason || detail.detail || detail.code || msg;
        } else if (Array.isArray(detail) && detail.length && detail[0].msg) {
          msg = detail[0].msg;
        }
        draft.error = msg;
      }
      render();
    });
  });
}

async function refreshTasks() {
  // Reset the SW stale marker. api() re-sets it to true if the
  // response carries X-OpsMemory-From-Cache (Codex chunk-6-close
  // blocker — without this reset the badge stuck on 'cached' even
  // after a successful network round-trip).
  state.sync.fromCache = false;
  try {
    const { tasks, total } = await loadTasks(state.filters);
    state.tasks = tasks;
    state.total = total;
    render();
  } catch (err) {
    renderError(err);
  }
  // Phase UI-3: fire dashboard refresh in parallel (don't block
  // tasks list render on it). Errors are swallowed — tiles render
  // a "—" placeholder if the request fails, but the tasks list
  // still appears.
  refreshDashboard().catch(() => {});
}

async function refreshDashboard() {
  // Skip when off the Tasks view (we re-fetch on tab change anyway).
  if (state.view !== 'tasks') return;
  const wantBiz = state.filters.business || 'all';
  state.dashboard.loading = true;
  state.dashboard.loadError = null;
  try {
    const params = new URLSearchParams();
    if (wantBiz && wantBiz !== 'all') params.set('business_slug', wantBiz);
    const data = await api('/v1/dashboard/summary'
      + (params.toString() ? '?' + params.toString() : ''));
    // Codex UI-3 R1 blocker: discard the response if the operator
    // changed the business filter while this request was in flight.
    // Without this check, a slow request for "borderline" can land
    // AFTER a fast request for "redhot" and overwrite the redhot
    // numbers with stale borderline data.
    if ((state.filters.business || 'all') !== wantBiz) return;
    state.dashboard.loaded = true;
    state.dashboard.loading = false;
    state.dashboard.loadError = null;
    state.dashboard.totals = data.totals || state.dashboard.totals;
    state.dashboard.open_aging = data.open_aging || state.dashboard.open_aging;
    state.dashboard.by_business = data.by_business || [];
    state.dashboard.spark_daily_done = data.spark_daily_done || [];
    state.dashboard.forBusiness = wantBiz;
  } catch (e) {
    if ((state.filters.business || 'all') !== wantBiz) return;
    state.dashboard.loading = false;
    state.dashboard.loadError = (e && e.message) || 'dashboard failed';
  }
  if (state.view === 'tasks') render();
}

async function refreshReviewItems() {
  try {
    const { items, total } = await loadReviewItems(state.review.statusFilter);
    state.review.items = items;
    state.review.total = total;
    render();
  } catch (err) {
    renderError(err);
  }
}

async function refreshSops() {
  try {
    const { items, total } = await loadSops({
      statusFilter: state.sops.statusFilter,
      businessFilter: state.sops.businessFilter,
    });
    state.sops.items = items;
    state.sops.total = total;
    render();
  } catch (err) {
    state.sops.loadError = err.message || 'Failed to load SOPs.';
    render();
  }
}

async function refreshAnchors() {
  state.anchors.loadError = null;
  try {
    const { items, total } = await loadAnchors({
      statusFilter: state.anchors.statusFilter,
      businessFilter: state.anchors.businessFilter,
    });
    state.anchors.items = items;
    state.anchors.total = total;
  } catch (err) {
    state.anchors.loadError = formatSopError('load anchors', err);
  }
  render();
}

function formatActionError(verb, err) {
  if (!err) return `Failed to ${verb}.`;
  if (err.kind === 'conflict') {
    const code = err.body && err.body.detail && err.body.detail.code;
    return `Conflict during ${verb}` + (code ? ` (${code})` : '') + '. Item demoted to needs_changes — see Last apply error.';
  }
  if (err.kind === 'validation') {
    return `Validation failed during ${verb}. Item demoted to needs_changes — see Last apply error.`;
  }
  if (err.kind === 'not_implemented') {
    return `Action not yet implemented.`;
  }
  return err.message || `Failed to ${verb}.`;
}

// Shared save flow used by both Save Templates and Publish (Publish
// auto-saves first when dirty per Codex chunk-7-step4-ui2 blocker #2).
// Returns true on save success (or no-op when not dirty), false on
// failure. Surfaces error in state.sops.editing.saveError.
async function _saveSopTemplatesFlow() {
  if (!state.sops.editing) return false;
  const ed = state.sops.editing;
  const sopId = ed.sopId;
  const versionNo = ed.versionNo;

  // Validate due_offset_days client-side per Codex chunk-7-step4-ui2 (b)
  // — server's 422 is a backstop, but clearer feedback up front.
  for (let i = 0; i < ed.templates.length; i++) {
    const t = ed.templates[i];
    if (t.due_offset_days != null) {
      if (typeof t.due_offset_days !== 'number'
          || t.due_offset_days < -3650
          || t.due_offset_days > 3650) {
        ed.saveError = `Row ${i + 1}: due_offset_days must be an integer between -3650 and 3650.`;
        render();
        return false;
      }
    }
  }

  // Drop empty rows (a user added a row then didn't fill it).
  const cleaned = ed.templates
    .filter(t => (t.summary || '').trim().length > 0)
    .map(t => ({
      summary: t.summary.trim(),
      description: t.description ? t.description.trim() : null,
      due_offset_days: t.due_offset_days,
      dependency_text: t.dependency_text ? t.dependency_text.trim() : null,
      category: t.category ? t.category.trim() : null,
      priority: t.priority ? t.priority.trim() : null,
      owner_role: t.owner_role ? t.owner_role.trim() : null,
    }));

  // Codex chunk-7-step4-ui2 (e): block save when cleaned would be
  // empty AND the version had non-zero templates already (would wipe
  // a previously valid draft). For a brand-new draft (zero templates
  // on server) saving empty is harmless but pointless — block too.
  if (cleaned.length === 0) {
    ed.saveError = 'Cannot save an empty template list. Add at least one row with a summary.';
    render();
    return false;
  }

  ed.pending = 'save';
  ed.saveError = null;
  render();
  try {
    await saveSopTemplates(sopId, versionNo, cleaned);
    const fresh = await loadSopVersionDetail(sopId, versionNo);
    if (state.sops.editing && state.sops.editing.sopId === sopId
        && state.sops.editing.versionNo === versionNo) {
      state.sops.selectedVersionDetail = fresh;
      state.sops.editing.templates = cleaned;
      state.sops.editing.dirty = false;
      state.sops.editing.pending = null;
    }
    render();
    return true;
  } catch (err) {
    if (state.sops.editing && state.sops.editing.sopId === sopId
        && state.sops.editing.versionNo === versionNo) {
      state.sops.editing.pending = null;
      state.sops.editing.saveError = formatSopError('save templates', err);
    }
    render();
    return false;
  }
}

// SOP-specific formatter (Codex chunk-7-step4-ui2 fix — formatActionError
// uses review-queue-specific phrasing about "demoted to needs_changes"
// which is wrong for SOP errors).
function formatSopError(verb, err) {
  if (!err) return `Failed to ${verb}.`;
  if (err.kind === 'conflict') {
    const code = err.body && err.body.detail && err.body.detail.code;
    return `Conflict during ${verb}` + (code ? ` (${code})` : '') +
           `. Refresh the SOP and try again.`;
  }
  if (err.kind === 'validation') {
    const detail = err.body && err.body.detail;
    if (typeof detail === 'string') return `Validation failed: ${detail}.`;
    return `Validation failed during ${verb}.`;
  }
  if (err.kind === 'not_implemented') {
    return `Action not yet implemented.`;
  }
  return err.message || `Failed to ${verb}.`;
}

// ---------------------------------------------------------------------------
// Outbox-backed mutations (Chunk 6 step 3)
// ---------------------------------------------------------------------------

const REPLAY_BACKOFF_BASE_MS = 5_000;
const REPLAY_BACKOFF_MAX_MS = 60_000;

async function refreshSyncCounters() {
  const Outbox = self.OpsMemoryOutbox;
  if (!Outbox) return;
  try {
    const pending = await Outbox.getByStatus('pending');
    const conflict = await Outbox.getByStatus('conflict');
    state.sync.pending = pending.length;
    state.sync.conflict = conflict.length;
    // Index by task_id so renderTask can show the conflict marker
    // (and future commits can show pending counts per task).
    const byTask = {};
    for (const m of pending) {
      byTask[m.task_id] = byTask[m.task_id] || {};
      byTask[m.task_id].pending = m;
    }
    for (const m of conflict) {
      byTask[m.task_id] = byTask[m.task_id] || {};
      byTask[m.task_id].conflict = m;
    }
    state.outboxByTask = byTask;
  } catch (e) {
    console.warn('[opsmemory] outbox count failed:', e);
  }
}

async function hydrateOptimisticFromOutbox() {
  // On PWA reload, restore the optimistic overlay for any 'pending'
  // mutations in the outbox so the UI matches what the user clicked
  // before the reload. Replay then resolves them.
  const Outbox = self.OpsMemoryOutbox;
  if (!Outbox) return;
  try {
    const pending = await Outbox.getByStatus('pending');
    for (const m of pending) {
      if (m.optimistic_patch) applyOptimistic(m.task_id, m.optimistic_patch);
    }
  } catch (e) {
    console.warn('[opsmemory] hydrate from outbox failed:', e);
  }
}

function applyOptimistic(taskId, patch) {
  state.optimistic[taskId] = Object.assign({}, state.optimistic[taskId] || {}, patch);
}

function clearOptimistic(taskId) {
  delete state.optimistic[taskId];
}

function effectiveTask(task) {
  // Overlay any in-flight optimistic patch over the server-known task.
  const patch = state.optimistic[task.id];
  if (!patch) return task;
  return Object.assign({}, task, patch);
}

function getTaskFromState(taskId) {
  return state.tasks.find(function (t) { return t.id === taskId; });
}

async function enqueueToggleDone(task) {
  const Outbox = self.OpsMemoryOutbox;
  if (!Outbox) {
    throw { kind: 'local', message: 'Outbox not available — reload the page.' };
  }
  const goingToDone = task.status !== 'done';
  // The PWA tasks list /v1/tasks doesn't currently return field
  // versions per task. Send empty base_field_versions; the server
  // skips the per-field compare (whole-task version is still
  // checked). Once /v1/tasks is extended to include field versions,
  // populate the relevant subset (status / completed_* / etc.).
  const body = {
    idempotency_key: Outbox.newKey(),
    base_task_version: task.version,
    base_field_versions: {},
    completion_note: null,
  };
  const previous_task_snapshot = {
    id: task.id,
    status: task.status,
    version: task.version,
    completed_at: task.completed_at,
    completed_by: task.completed_by,
    completion_note: task.completion_note,
  };
  const optimistic_patch = goingToDone
    ? { status: 'done' }
    : { status: 'open', completed_at: null, completed_by: null, completion_note: null };

  await Outbox.enqueue({
    idempotency_key: body.idempotency_key,
    principal_id: state.principal && state.principal.id,
    action: 'toggle_done',
    method: 'POST',
    path: '/v1/tasks/' + encodeURIComponent(task.id) + '/toggle_done',
    task_id: task.id,
    body: body,
    base_task_version: body.base_task_version,
    base_field_versions: body.base_field_versions,
    previous_task_snapshot: previous_task_snapshot,
    optimistic_patch: optimistic_patch,
  });
  applyOptimistic(task.id, optimistic_patch);
  await refreshSyncCounters();
  render();
  // Try to flush immediately when online; offline replay fires on
  // window 'online'.
  replayOutbox().catch(function (e) {
    console.warn('[opsmemory] replayOutbox after enqueue failed:', e);
  });
}

let _replayInFlight = false;

async function replayOutbox() {
  if (_replayInFlight) return;
  if (!navigator.onLine) return;
  const Outbox = self.OpsMemoryOutbox;
  if (!Outbox) return;
  _replayInFlight = true;
  try {
    const pending = await Outbox.getByStatus('pending');
    // Sort by created_at to replay in order (the server's idempotency
    // ledger doesn't care, but the user's expectation does).
    pending.sort(function (a, b) {
      return (a.created_at || '').localeCompare(b.created_at || '');
    });
    const now = Date.now();
    for (const m of pending) {
      // Backoff gate.
      if (m.next_attempt_at && Date.parse(m.next_attempt_at) > now) continue;
      await replayOne(m);
    }
  } finally {
    _replayInFlight = false;
    await refreshSyncCounters();
    render();
  }
}

async function replayOne(m) {
  const Outbox = self.OpsMemoryOutbox;
  if (m.action !== 'toggle_done') {
    // Future actions land in subsequent commits; for now the only
    // outbox shape is toggle_done.
    await Outbox.update(m.idempotency_key, {
      status: 'rejected',
      last_error: 'unknown_action_in_outbox',
    });
    clearOptimistic(m.task_id);
    return;
  }
  try {
    const result = await postToggleDone(m.task_id, m.body);
    await Outbox.update(m.idempotency_key, {
      status: 'applied',
      last_status: 200,
      server_payload: result,
    });
    // Pull server state into the local task list. Don't blindly
    // refresh the whole list (would erase other in-flight optimistic
    // patches); patch the affected task.
    const local = getTaskFromState(m.task_id);
    if (local) {
      Object.assign(local, {
        status: result.status,
        version: result.version,
        completed_at: result.completed_at || null,
        completed_by: result.completed_by || null,
        completion_note: result.completion_note || null,
      });
    }
    clearOptimistic(m.task_id);
  } catch (err) {
    await handleReplayError(m, err);
  }
}

async function handleReplayError(m, err) {
  const Outbox = self.OpsMemoryOutbox;
  if (err.kind === 'conflict') {
    // The server has a different version. Pull canonical state so the
    // user can decide. Revert the optimistic patch.
    let server_task = null;
    try { server_task = await refetchTask(m.task_id); } catch (e) {
      console.warn('[opsmemory] refetch after 409 failed:', e);
    }
    await Outbox.update(m.idempotency_key, {
      status: 'conflict',
      last_status: 409,
      last_error: (err.body && err.body.detail && err.body.detail.code) || 'task_version_moved',
      server_payload: server_task || (err.body || null),
      attempt_count: (m.attempt_count || 0) + 1,
    });
    if (server_task) {
      const local = getTaskFromState(m.task_id);
      if (local) Object.assign(local, server_task);
    }
    clearOptimistic(m.task_id);
    return;
  }
  if (err.kind === 'validation' || err.kind === 'not_found' ||
      err.kind === 'forbidden' || err.kind === 'auth' ||
      err.kind === 'not_implemented') {
    // Deterministic server reject. Don't retry; revert optimistic UI.
    await Outbox.update(m.idempotency_key, {
      status: 'rejected',
      last_status: err.status || null,
      last_error: (err.body && err.body.detail && err.body.detail.code) || err.kind,
      server_payload: err.body || null,
      attempt_count: (m.attempt_count || 0) + 1,
    });
    clearOptimistic(m.task_id);
    return;
  }
  // Transient: network throw or 5xx. Schedule backoff retry.
  const attempt = (m.attempt_count || 0) + 1;
  const delay = Math.min(REPLAY_BACKOFF_MAX_MS,
                          REPLAY_BACKOFF_BASE_MS * Math.pow(2, attempt - 1));
  await Outbox.update(m.idempotency_key, {
    last_status: err.status || null,
    last_error: err.message || 'network_error',
    attempt_count: attempt,
    next_attempt_at: new Date(Date.now() + delay).toISOString(),
  });
}

async function discardConflict(idempotency_key) {
  const Outbox = self.OpsMemoryOutbox;
  if (!Outbox) return;
  await Outbox.discard(idempotency_key);
  await refreshSyncCounters();
  render();
}

async function retryConflict(idempotency_key) {
  const Outbox = self.OpsMemoryOutbox;
  if (!Outbox) return;
  const m = await Outbox.getByKey(idempotency_key);
  if (!m) return;
  // Rebuild from current server state. The user is saying "now try
  // again" — so re-base on whatever the server has.
  const task = getTaskFromState(m.task_id);
  if (!task) {
    await Outbox.update(idempotency_key, { status: 'discarded',
                                            last_error: 'task_not_in_view' });
    await refreshSyncCounters();
    render();
    return;
  }
  // Discard the old conflicted mutation, enqueue a fresh one.
  await Outbox.discard(idempotency_key);
  await enqueueToggleDone(task);
}

// ---------------------------------------------------------------------------
// Service worker (chunk 1: pass-through; chunk 6 will add caching)
// ---------------------------------------------------------------------------

// Sub-(c) commit 2: deep-link auto-expand helpers.
//
// Codex chunk-10-step3b3-close SUB-(c) PLAN: parse ?task=<id>
// from window.location, set view='tasks' + status='all', refresh,
// expand. If the task isn't in the rendered list, fetch it
// directly so the expanded-detail panel still renders.
function _readDeepLinkTaskId() {
  try {
    const params = new URLSearchParams(window.location.search || '');
    const id = params.get('task');
    if (!id) return null;
    // UUID v4 sanity check — reject anything that doesn't look
    // like a UUID so a malformed query doesn't trigger a /v1/tasks
    // /{garbage} 422.
    const trimmed = id.trim();
    if (!/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(trimmed)) {
      return null;
    }
    return trimmed;
  } catch (_e) {
    return null;
  }
}

async function _autoExpandDeepLinkedTask(taskId) {
  // Try the in-list path first. refreshTasks() has already
  // populated state.tasks; if our taskId is among them, expand
  // and load detail through the existing path.
  const inList = (state.tasks || []).some(t => t && t.id === taskId);
  if (inList) {
    state.expandedTaskId = taskId;
    try {
      state.expandedTaskDetail = await loadTaskDetail(taskId);
    } catch (_e) { /* keep row expanded; user can click again */ }
    render();
    return;
  }
  // Not in the visible list. Fetch the detail directly so the
  // user at least sees the task they were notified about. The
  // list row won't render, but the expanded detail will.
  try {
    const detail = await loadTaskDetail(taskId);
    state.expandedTaskId = taskId;
    state.expandedTaskDetail = detail;
    // Inject a synthetic stub into state.tasks so the renderer
    // has a row to anchor the expanded detail under.
    //
    // Codex chunk-10-step3c-close BLOCKER: the stub MUST mirror
    // the shape of a list-row task (especially `version`),
    // because Mark done / Reopen reads the row from state.tasks
    // and sends `base_task_version: task.version` per
    // api/app/v1_mutations.py. JSON.stringify drops undefined
    // fields, so a stub without `version` ships a body the
    // server 422s on. Forward every field the detail returned;
    // unknown fields are harmless on the renderer side.
    const stubRow = { ...detail, _deep_link_only: true };
    state.tasks = [stubRow, ...(state.tasks || [])];
    render();
  } catch (err) {
    // 404 / 403 / network — leave the user on the (filtered)
    // task list. No notification banner; the failed deep link
    // is silent enough.
    console.warn('[opsmemory] deep-link task fetch failed', taskId, err);
    render();
  }
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try { await navigator.serviceWorker.register('/sw.js', { scope: '/' }); }
  catch (err) { console.warn('SW registration failed:', err); }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

function attachConnectivityHandlers() {
  function setOnline(online) {
    state.sync.online = online;
    render();
    if (online) {
      // Try to flush any pending mutations the moment we're back.
      replayOutbox().catch(function () {});
    }
  }
  window.addEventListener('online', function () { setOnline(true); });
  window.addEventListener('offline', function () { setOnline(false); });
  // Also poll on visibilitychange — a tab brought back to foreground
  // after a long sleep may have stale connectivity state.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && navigator.onLine) {
      replayOutbox().catch(function () {});
    }
  });
}

async function _bootPrincipal() {
  // Try network first; on failure, fall back to persisted last-known
  // principal so the PWA renders an offline shell with the cached
  // optimistic state instead of the generic error screen.
  try {
    return await loadPrincipal();
  } catch (err) {
    const persisted = _readPersistedPrincipal();
    if (persisted) {
      state.sync.online = false;
      state.sync.fromCache = true;
      console.warn('[opsmemory] /whoami failed; using persisted principal', err);
      return persisted;
    }
    throw err;
  }
}

async function _bootBusinesses() {
  try {
    return await loadBusinesses();
  } catch (err) {
    const persisted = _readPersistedBusinesses();
    if (persisted && persisted.length) {
      console.warn('[opsmemory] /v1/businesses failed; using persisted', err);
      return persisted;
    }
    return [];
  }
}

// =====================================================================
// Phase UI-1 (2026-05-09) — keyboard navigation, selection, action
// shortcuts, command palette stub, ? overlay.
//
// Mostly self-contained: one document-level keydown listener routes to
// per-view handlers; one document-level click listener picks up the
// bulk-bar buttons and the row-checkbox clicks via event delegation.
// State lives on state.review.{focusedIndex, selectedIds, ...}.
// =====================================================================

const _UI1 = {
  // Tracks "g <next>" multi-key sequences. Set when 'g' is pressed,
  // cleared after the next keypress (or after 1s). Used for
  // 'g r' / 'g t' / 'g s' navigation a la Linear / GitHub.
  gPending: false,
  gTimer: null,
  // True while the ? overlay is open.
  shortcutsOpen: false,
  // True while the Cmd+K palette is open.
  paletteOpen: false,
};

function _ui1IsTypingTarget(el) {
  if (!el) return false;
  const tag = (el.tagName || '').toUpperCase();
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (el.isContentEditable) return true;
  return false;
}

function _ui1FocusRow(newIndex) {
  const items = state.review.items;
  if (!items.length) {
    state.review.focusedIndex = null;
    return;
  }
  const clamped = Math.max(0, Math.min(items.length - 1, newIndex));
  state.review.focusedIndex = clamped;
  // After re-render, scroll the focused row into view.
  requestAnimationFrame(() => {
    const el = document.querySelector(`.rv-item.focused`);
    if (el && el.scrollIntoView) {
      el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  });
}

function _ui1ToggleSelectionAtIndex(index) {
  const item = state.review.items[index];
  if (!item) return;
  if (state.review.selectedIds.has(item.id)) {
    state.review.selectedIds.delete(item.id);
  } else {
    state.review.selectedIds.add(item.id);
  }
}

function _ui1ClearSelection() {
  state.review.selectedIds = new Set();
}

function _ui1SelectAll() {
  state.review.selectedIds = new Set(state.review.items.map(i => i.id));
}

async function _ui1RunApproveOnTargets(ids) {
  if (!ids.length || state.review.bulkInProgress) return;
  state.review.bulkInProgress = true;
  state.review.actionError = null;
  render();
  let okCount = 0;
  const errors = [];
  for (const id of ids) {
    try {
      await approveReview(id);
      okCount++;
    } catch (err) {
      errors.push({ id, message: err.message || 'approve failed' });
    }
  }
  state.review.bulkInProgress = false;
  state.review.selectedIds = new Set();
  state.review.actionError = errors.length
    ? `Approved ${okCount}/${ids.length}; ${errors.length} failed (${errors[0].message}).`
    : null;
  // Refetch the queue so completed items drop out.
  try {
    const result = await loadReviewItems(state.review.statusFilter);
    state.review.items = result.items;
    state.review.total = result.total;
    if (state.review.focusedIndex !== null) {
      state.review.focusedIndex = Math.min(state.review.focusedIndex, state.review.items.length - 1);
      if (state.review.focusedIndex < 0) state.review.focusedIndex = null;
    }
  } catch (_e) { /* leave stale list */ }
  render();
}

async function _ui1RunRejectOnTargets(ids) {
  if (!ids.length || state.review.bulkInProgress) return;
  const reason = window.prompt(
    ids.length === 1
      ? 'Reject reason (optional):'
      : `Reject ${ids.length} items — reason applied to all (optional):`,
    ''
  );
  if (reason === null) return;  // user cancelled
  state.review.bulkInProgress = true;
  state.review.actionError = null;
  render();
  let okCount = 0;
  const errors = [];
  for (const id of ids) {
    try {
      await rejectReview(id, reason);
      okCount++;
    } catch (err) {
      errors.push({ id, message: err.message || 'reject failed' });
    }
  }
  state.review.bulkInProgress = false;
  state.review.selectedIds = new Set();
  state.review.actionError = errors.length
    ? `Rejected ${okCount}/${ids.length}; ${errors.length} failed (${errors[0].message}).`
    : null;
  try {
    const result = await loadReviewItems(state.review.statusFilter);
    state.review.items = result.items;
    state.review.total = result.total;
    if (state.review.focusedIndex !== null) {
      state.review.focusedIndex = Math.min(state.review.focusedIndex, state.review.items.length - 1);
      if (state.review.focusedIndex < 0) state.review.focusedIndex = null;
    }
  } catch (_e) { /* leave stale list */ }
  render();
}

function _ui1ActionTargets() {
  // Action shortcuts (1/2) target: selected ids if ≥1, else focused single.
  if (state.review.selectedIds.size > 0) {
    return Array.from(state.review.selectedIds);
  }
  if (state.review.focusedIndex !== null) {
    const visible = state.review.items_visible || state.review.items;
    const item = visible[state.review.focusedIndex];
    return item ? [item.id] : [];
  }
  return [];
}

function _ui1ToggleShortcutsOverlay() {
  _UI1.shortcutsOpen = !_UI1.shortcutsOpen;
  const existing = document.getElementById('ui1-shortcuts-overlay');
  if (_UI1.shortcutsOpen) {
    if (existing) return;
    const el = document.createElement('div');
    el.id = 'ui1-shortcuts-overlay';
    el.className = 'ui1-overlay';
    el.innerHTML = `
      <div class="ui1-overlay-card">
        <h2>Keyboard shortcuts</h2>
        <table class="ui1-shortcuts-table">
          <tr><td><kbd>J</kbd> / <kbd>↓</kbd></td><td>Move focus down</td></tr>
          <tr><td><kbd>K</kbd> / <kbd>↑</kbd></td><td>Move focus up</td></tr>
          <tr><td><kbd>Enter</kbd></td><td>Expand / open focused row</td></tr>
          <tr><td><kbd>Esc</kbd></td><td>Collapse detail · clear selection · close overlay</td></tr>
          <tr><td><kbd>X</kbd></td><td>Toggle selection on focused row</td></tr>
          <tr><td><kbd>Shift</kbd>+<kbd>J</kbd>/<kbd>K</kbd></td><td>Extend selection</td></tr>
          <tr><td><kbd>Ctrl/Cmd</kbd>+<kbd>A</kbd></td><td>Select all visible</td></tr>
          <tr><td><kbd>1</kbd></td><td>Approve focused/selected</td></tr>
          <tr><td><kbd>2</kbd></td><td>Reject focused/selected (prompts for reason)</td></tr>
          <tr><td><kbd>G</kbd> then <kbd>R</kbd></td><td>Go to Review</td></tr>
          <tr><td><kbd>G</kbd> then <kbd>T</kbd></td><td>Go to Tasks</td></tr>
          <tr><td><kbd>G</kbd> then <kbd>S</kbd></td><td>Go to SOPs</td></tr>
          <tr><td><kbd>Ctrl/Cmd</kbd>+<kbd>K</kbd></td><td>Command palette</td></tr>
          <tr><td><kbd>?</kbd></td><td>Show this overlay</td></tr>
        </table>
        <div class="ui1-overlay-hint">Press <kbd>Esc</kbd> or <kbd>?</kbd> to close.</div>
      </div>
    `;
    document.body.appendChild(el);
    el.addEventListener('click', (ev) => {
      if (ev.target === el) _ui1ToggleShortcutsOverlay();
    });
  } else if (existing) {
    existing.remove();
  }
}

function _ui1TogglePalette() {
  _UI1.paletteOpen = !_UI1.paletteOpen;
  const existing = document.getElementById('ui1-palette-overlay');
  if (_UI1.paletteOpen) {
    if (existing) return;
    const el = document.createElement('div');
    el.id = 'ui1-palette-overlay';
    el.className = 'ui1-overlay';
    el.innerHTML = `
      <div class="ui1-overlay-card ui1-palette-card">
        <h2>Quick navigation</h2>
        <ul class="ui1-palette-list">
          <li data-nav="review"><kbd>G R</kbd> · Review queue</li>
          <li data-nav="tasks"><kbd>G T</kbd> · Tasks</li>
          <li data-nav="sops"><kbd>G S</kbd> · SOPs</li>
          <li data-nav="settings">·  Settings</li>
        </ul>
        <div class="ui1-overlay-hint">Real fuzzy search lands in UI-2. Press <kbd>Esc</kbd> to close.</div>
      </div>
    `;
    document.body.appendChild(el);
    el.addEventListener('click', (ev) => {
      const li = ev.target.closest('[data-nav]');
      if (li) {
        state.view = li.dataset.nav;
        _ui1TogglePalette();
        render();
        return;
      }
      if (ev.target === el) _ui1TogglePalette();
    });
  } else if (existing) {
    existing.remove();
  }
}

function _ui1HandleKey(e) {
  const isMeta = e.ctrlKey || e.metaKey;

  // Codex B3-3 blocker: dedup modal keys (1-5 pick row, N add anyway,
  // Esc back) must fire ABOVE the typing-target bail. Otherwise an
  // operator who left focus on the underlying summary input gets
  // their keystrokes eaten by the input. preventDefault() prevents
  // the digit/letter from also inserting into the focused input.
  if (state.quickAdd && state.quickAdd.open && state.quickAdd.dedup
      && !isMeta) {
    const dedup = state.quickAdd.dedup;
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      state.quickAdd.dedup = null;
      render();
      return;
    }
    if (e.key >= '1' && e.key <= '5') {
      const idx = parseInt(e.key, 10) - 1;
      const cand = (dedup.candidates || [])[idx];
      if (cand) {
        e.preventDefault();
        e.stopPropagation();
        const taskId = cand.id;
        state.quickAdd = {
          open: false, summary: '', businessSlug: '', dueAtLocal: '',
          description: '', kind: 'task', submitting: false, error: null,
          ownerUserId: '', members: [], membersLoading: false, dedup: null,
        };
        state.view = 'tasks';
        try { window.history.replaceState(null, '',
                                          `/?task=${encodeURIComponent(taskId)}`); }
        catch (_e) {}
        render();
        if (typeof refreshTasks === 'function') {
          refreshTasks().then(() => {
            if (typeof _autoExpandDeepLinkedTask === 'function') {
              _autoExpandDeepLinkedTask(taskId).catch(() => {});
            }
          }).catch(() => {});
        }
        if (typeof tgShowToast === 'function') {
          tgShowToast('Opened existing task', 'ok');
        }
        return;
      }
    }
    if (e.key === 'n' || e.key === 'N') {
      e.preventDefault();
      e.stopPropagation();
      const previewToken = dedup.previewToken;
      state.quickAdd.dedup = null;
      const snap = _tgSnapshotQuickAddForm();
      let dueAtIso = null;
      if (snap.dueAtLocal) {
        const d = new Date(snap.dueAtLocal);
        if (!Number.isNaN(d.getTime())) dueAtIso = d.toISOString();
      }
      _tgCommitQuickAdd({ snap, dueAtIso, previewToken,
                          forceCreate: true });
      return;
    }
  }

  // B4 blocker: when an edit form is open, do NOT let global Triage
  // shortcuts (1 = approve, 2 = reject, X = toggle, J/K = navigate,
  // etc.) fire from a body-focused state. The race is: render() wipes
  // the focused input, focus moves to body, operator types '1' and
  // approves the unsaved original proposal. Esc to cancel, Save
  // button to commit.
  if (state.review && state.review.editingId
      && !state.quickAdd?.dedup
      && e.key !== 'Escape'
      && !_ui1IsTypingTarget(e.target)) {
    return;
  }

  // Bail on typing targets.
  if (_ui1IsTypingTarget(e.target)) {
    // Quick Add submit shortcut: Cmd/Ctrl+Enter only. Plain Enter
    // is intentionally NOT a submit — iOS keyboard's "return" key
    // fires Enter, which used to auto-submit the form. The submit
    // would take ~5s on a slow link and then close the modal,
    // which to the operator looked like "I typed something and
    // after 5 seconds it just disappeared." Operators tap the
    // explicit Add button (or use Cmd/Ctrl+Enter on desktop).
    if (e.key === 'Enter' && isMeta
        && state.quickAdd && state.quickAdd.open
        && !state.quickAdd.dedup
        && e.target && e.target.id !== 'tg-qa-description') {
      e.preventDefault();
      _tgSubmitQuickAdd();
    }
    return;
  }
  // Don't intercept browser shortcuts that include modifiers we
  // don't own (Alt, Meta+X for cut, etc.) unless we explicitly want
  // them.

  // Q opens Quick Add from any view (works on Tasks, SOPs, Settings,
  // Triage). Skipped if a modal is already open.
  if ((e.key === 'q' || e.key === 'Q') && !isMeta) {
    if (state.quickAdd && state.quickAdd.open) return;
    if (state.review && (state.review.rejectModal || state.review.snoozeModal)) return;
    e.preventDefault();
    tgOpenQuickAdd();
    return;
  }

  // (Dedup modal keys handled at top of this function above the
  // typing-target bail.)

  // Handle the 'g <next>' two-key sequence first.
  if (_UI1.gPending) {
    _UI1.gPending = false;
    if (_UI1.gTimer) { clearTimeout(_UI1.gTimer); _UI1.gTimer = null; }
    if (e.key === 'r' || e.key === 'R') {
      e.preventDefault(); state.view = 'review'; render(); return;
    }
    if (e.key === 't' || e.key === 'T') {
      e.preventDefault(); state.view = 'tasks'; render(); return;
    }
    if (e.key === 's' || e.key === 'S') {
      e.preventDefault();
      // Only admins see the SOPs tab in the existing nav; honor that.
      if (_isPlatformAdmin(state.principal)) {
        state.view = 'sops'; render();
      }
      return;
    }
    // Otherwise fall through to normal handling.
  }

  // Esc: close overlays first; else clear modal/search/forecast/selection.
  if (e.key === 'Escape') {
    if (_UI1.shortcutsOpen) { _ui1ToggleShortcutsOverlay(); e.preventDefault(); return; }
    if (_UI1.paletteOpen) { _ui1TogglePalette(); e.preventDefault(); return; }
    if (state.quickAdd && state.quickAdd.open && !state.quickAdd.submitting) {
      state.quickAdd.open = false;
      state.quickAdd.error = null;
      e.preventDefault();
      render();
      return;
    }
    if (state.view === 'review') {
      let changed = false;
      // Modals first (any open one), in priority order
      if (state.review.snoozeModal) {
        state.review.snoozeModal = null;
        changed = true;
      }
      else if (state.review.rejectModal) {
        state.review.rejectModal = null;
        changed = true;
      }
      // B4: edit mode counts as a "modal" for Esc priority.
      else if (state.review.editingId && !state.review.editSaving) {
        state.review.editingId = null;
        state.review.editDraft = null;
        state.review.editError = null;
        changed = true;
      }
      // Then search filter
      else if (state.review.searchQuery) {
        state.review.searchQuery = '';
        changed = true;
      }
      // Then forecast filter
      else if (state.review.forecastFilter) {
        state.review.forecastFilter = null;
        changed = true;
      }
      // Then selection
      else if (state.review.selectedIds.size > 0) {
        state.review.selectedIds = new Set();
        changed = true;
      }
      // Then expanded legacy detail
      else if (state.review.expandedId !== null) {
        state.review.expandedId = null;
        state.review.expandedDetail = null;
        changed = true;
      }
      if (changed) { render(); e.preventDefault(); }
    }
    return;
  }

  // Cmd+K palette
  if (isMeta && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    _ui1TogglePalette();
    return;
  }

  // ? overlay
  if (e.key === '?' && !isMeta) {
    e.preventDefault();
    _ui1ToggleShortcutsOverlay();
    return;
  }

  // 'g' starts a sequence
  if ((e.key === 'g' || e.key === 'G') && !isMeta) {
    e.preventDefault();
    _UI1.gPending = true;
    _UI1.gTimer = setTimeout(() => { _UI1.gPending = false; _UI1.gTimer = null; }, 1200);
    return;
  }

  // Cmd+A: select all visible (review tab only)
  if (isMeta && (e.key === 'a' || e.key === 'A') && state.view === 'review' && state.review.items.length) {
    e.preventDefault();
    const visible = state.review.items_visible || state.review.items;
    state.review.selectedIds = new Set(visible.map(i => i.id));
    render();
    return;
  }

  // The rest are review-tab-only.
  if (state.view !== 'review') return;

  // / opens search (focus the search input). Captured before the
  // visible-list bail-out so search works on an empty queue too.
  if (e.key === '/' && !isMeta) {
    e.preventDefault();
    const inp = document.getElementById('tg-search-input');
    if (inp) inp.focus();
    return;
  }

  // 3 = enter inline edit mode for the focused row (B4 active).
  if (e.key === '3' && !isMeta) {
    e.preventDefault();
    const visible = state.review.items_visible || [];
    const focused = state.review.focusedIndex != null
      ? visible[state.review.focusedIndex]
      : null;
    if (!focused) {
      tgShowToast('Focus a row first', 'err');
      return;
    }
    if (focused.status !== 'pending' && focused.status !== 'needs_changes') {
      tgShowToast('Only pending items can be edited', 'err');
      return;
    }
    _tgEnterEditMode(focused);
    return;
  }
  // H = snooze. Targets selected ids if >=1, else focused single.
  if ((e.key === 'h' || e.key === 'H') && !isMeta) {
    e.preventDefault();
    const sel = state.review.selectedIds;
    const visible = state.review.items_visible || [];
    let ids = [];
    if (sel && sel.size > 0) {
      ids = Array.from(sel);
    } else if (state.review.focusedIndex != null && visible[state.review.focusedIndex]) {
      ids = [visible[state.review.focusedIndex].id];
    }
    if (ids.length === 0) {
      tgShowToast('No row to snooze — focus or select first', 'err');
      return;
    }
    tgOpenSnoozeModal(ids);
    return;
  }

  // Phase UI-2 edit: navigation + selection use items_visible (the
  // filtered subset under the current sub-view + forecast + search),
  // not the raw items[]. focusedIndex indexes into items_visible.
  const visible = state.review.items_visible || [];
  if (!visible.length) return;

  const cur = state.review.focusedIndex;
  const last = visible.length - 1;

  // Movement: J/K + arrows
  if (e.key === 'j' || e.key === 'J' || e.key === 'ArrowDown') {
    e.preventDefault();
    const next = cur === null ? 0 : Math.min(cur + 1, last);
    if (e.shiftKey && cur !== null) {
      const oldItem = visible[cur];
      const newItem = visible[next];
      if (oldItem) state.review.selectedIds.add(oldItem.id);
      if (newItem) state.review.selectedIds.add(newItem.id);
    }
    state.review.focusedIndex = next;
    render();
    requestAnimationFrame(() => {
      const el = document.querySelector('.tg-row.focused');
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
    });
    return;
  }
  if (e.key === 'k' || e.key === 'K' || e.key === 'ArrowUp') {
    e.preventDefault();
    const next = cur === null ? 0 : Math.max(cur - 1, 0);
    if (e.shiftKey && cur !== null) {
      const oldItem = visible[cur];
      const newItem = visible[next];
      if (oldItem) state.review.selectedIds.add(oldItem.id);
      if (newItem) state.review.selectedIds.add(newItem.id);
    }
    state.review.focusedIndex = next;
    render();
    requestAnimationFrame(() => {
      const el = document.querySelector('.tg-row.focused');
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
    });
    return;
  }

  // X: toggle selection on focused row
  if ((e.key === 'x' || e.key === 'X') && !isMeta && cur !== null) {
    e.preventDefault();
    const item = visible[cur];
    if (item) {
      if (state.review.selectedIds.has(item.id)) state.review.selectedIds.delete(item.id);
      else state.review.selectedIds.add(item.id);
    }
    render();
    return;
  }

  // Enter: open detail (it's the right-side pane in Triage so this
  // toggles the detail-pane visibility on narrow viewports).
  if (e.key === 'Enter' && cur !== null) {
    e.preventDefault();
    state.review.detailOpen = true;
    render();
    return;
  }

  // 1: approve focused/selected
  if (e.key === '1' && !isMeta) {
    e.preventDefault();
    const ids = _ui1ActionTargets();
    if (ids.length === 0) return;
    if (ids.length > 1) {
      const ok = window.confirm(`Approve ${ids.length} items?`);
      if (!ok) return;
    }
    _ui1RunApproveOnTargets(ids);
    return;
  }

  // 2: reject focused/selected
  if (e.key === '2' && !isMeta) {
    e.preventDefault();
    const ids = _ui1ActionTargets();
    if (ids.length === 0) return;
    _ui1RunRejectOnTargets(ids);
    return;
  }
}

function _ui1HandleClick(e) {
  // ---- Phase UI-2: Triage surfaces -----------------------------------

  // Sub-tab switch
  const subtab = e.target.closest('[data-tg-subtab]');
  if (subtab) {
    state.review.subview = subtab.dataset.tgSubtab;
    state.review.focusedIndex = null;  // reset focus on sub-view change
    state.review.forecastFilter = null;  // reset forecast on sub-view change
    render();
    return;
  }

  // Forecast card toggle
  const fc = e.target.closest('[data-tg-forecast]');
  if (fc) {
    const k = fc.dataset.tgForecast;
    state.review.forecastFilter = state.review.forecastFilter === k ? null : k;
    render();
    return;
  }

  // Triage row checkbox toggles selection without focus jump
  const tgcb = e.target.closest('.tg-checkmark');
  if (tgcb) {
    const row = tgcb.closest('.tg-row[data-tg-index]');
    if (row) {
      const idx = parseInt(row.dataset.tgIndex, 10);
      const visible = state.review.items_visible || [];
      const item = visible[idx];
      if (item) {
        if (state.review.selectedIds.has(item.id)) state.review.selectedIds.delete(item.id);
        else state.review.selectedIds.add(item.id);
        e.stopPropagation();
        render();
      }
    }
    return;
  }

  // Triage row click → focus that row + open detail
  const tgrow = e.target.closest('.tg-row[data-tg-index]');
  if (tgrow) {
    const idx = parseInt(tgrow.dataset.tgIndex, 10);
    if (!Number.isNaN(idx)) {
      state.review.focusedIndex = idx;
      state.review.detailOpen = true;
      render();
    }
    return;
  }

  // Triage detail-pane buttons (single-item action on focused)
  if (e.target.closest('[data-tg-detail-approve]')) {
    const ids = _ui1ActionTargets();
    if (ids.length === 0) return;
    if (ids.length > 1) {
      const ok = window.confirm(`Approve ${ids.length} items?`);
      if (!ok) return;
    }
    _ui1RunApproveOnTargets(ids);
    return;
  }
  if (e.target.closest('[data-tg-detail-reject]')) {
    const ids = _ui1ActionTargets();
    if (ids.length === 0) return;
    state.review.rejectModal = { ids, preset: null, reasonText: '' };
    render();
    return;
  }
  if (e.target.closest('[data-tg-detail-edit]')) {
    const focused = state.review.items_visible
      && state.review.items_visible[state.review.focusedIndex];
    if (focused) _tgEnterEditMode(focused);
    return;
  }
  if (e.target.closest('[data-tg-edit-cancel]')) {
    if (state.review.editSaving) return;
    state.review.editingId = null;
    state.review.editDraft = null;
    state.review.editError = null;
    render();
    return;
  }
  if (e.target.closest('[data-tg-edit-save]')) {
    if (state.review.editSaving) return;
    _tgSaveEdit();
    return;
  }
  if (e.target.closest('[data-tg-detail-snooze]')) {
    const ids = _ui1ActionTargets();
    if (ids.length === 0) return;
    tgOpenSnoozeModal(ids);
    return;
  }
  // Detail-pane unsnooze (only present when item is currently snoozed)
  if (e.target.closest('[data-tg-detail-unsnooze]')) {
    const focused = state.review.items_visible
      && state.review.items_visible[state.review.focusedIndex];
    if (focused) _tgUnsnooze(focused.id);
    return;
  }

  // Triage bulk-bar buttons
  if (e.target.closest('[data-tg-bulk-approve]')) {
    const ids = Array.from(state.review.selectedIds);
    if (ids.length > 1) {
      const ok = window.confirm(`Approve ${ids.length} items?`);
      if (!ok) return;
    }
    _ui1RunApproveOnTargets(ids);
    return;
  }
  if (e.target.closest('[data-tg-bulk-reject]')) {
    const ids = Array.from(state.review.selectedIds);
    if (ids.length === 0) return;
    state.review.rejectModal = { ids, preset: null, reasonText: '' };
    render();
    return;
  }
  if (e.target.closest('[data-tg-bulk-snooze]')) {
    const ids = Array.from(state.review.selectedIds);
    if (ids.length === 0) return;
    tgOpenSnoozeModal(ids);
    return;
  }
  if (e.target.closest('[data-tg-bulk-clear]')) {
    state.review.selectedIds = new Set();
    render();
    return;
  }

  // Reject modal interactions
  if (e.target.closest('[data-tg-modal-cancel]')
      || (e.target.matches('[data-tg-modal-backdrop]') && state.review.rejectModal)) {
    state.review.rejectModal = null;
    render();
    return;
  }
  const reason = e.target.closest('[data-tg-reason]');
  if (reason && state.review.rejectModal) {
    state.review.rejectModal.preset = reason.dataset.tgReason;
    if (!state.review.rejectModal.reasonText) {
      state.review.rejectModal.reasonText = reason.dataset.tgReason;
    }
    render();
    return;
  }
  if (e.target.closest('[data-tg-modal-submit]') && state.review.rejectModal) {
    const m = state.review.rejectModal;
    const ta = document.getElementById('tg-reject-textarea');
    const reason = (ta ? ta.value : (m.reasonText || m.preset || '')).trim();
    const ids = m.ids;
    state.review.rejectModal = null;
    render();
    _ui1RunRejectOnTargetsWithReason(ids, reason);
    return;
  }

  // Snooze modal interactions
  if (e.target.closest('[data-tg-snooze-cancel]')
      || (e.target.matches('[data-tg-modal-backdrop]') && state.review.snoozeModal)) {
    state.review.snoozeModal = null;
    render();
    return;
  }
  const tile = e.target.closest('[data-tg-snooze-tile]');
  if (tile && state.review.snoozeModal) {
    state.review.snoozeModal.selectedKey = tile.dataset.tgSnoozeTile;
    render();
    return;
  }
  if (e.target.closest('[data-tg-snooze-submit]') && state.review.snoozeModal) {
    // Capture current textarea + datetime-local values into state
    // before _tgApplySnooze reads them (modal will close mid-flight).
    const ta = document.getElementById('tg-snooze-textarea');
    const cu = document.getElementById('tg-snooze-custom');
    if (ta) state.review.snoozeModal.reasonText = ta.value;
    if (cu) state.review.snoozeModal.customIso = cu.value;
    _tgApplySnooze();
    return;
  }

  // ---- Phase UI-1 legacy review-tab list ----------------------------
  // Checkbox click on a row toggles selection without opening detail.
  const cb = e.target.closest('.rv-checkbox');
  if (cb) {
    const li = cb.closest('.rv-item[data-review-index]');
    if (li) {
      const idx = parseInt(li.dataset.reviewIndex, 10);
      if (!Number.isNaN(idx)) {
        e.stopPropagation();
        _ui1ToggleSelectionAtIndex(idx);
        render();
      }
    }
    return;
  }
  // Bulk bar buttons (legacy class)
  if (e.target.classList.contains('rv-bulk-approve')) {
    const ids = Array.from(state.review.selectedIds);
    if (ids.length > 1) {
      const ok = window.confirm(`Approve ${ids.length} items?`);
      if (!ok) return;
    }
    _ui1RunApproveOnTargets(ids);
    return;
  }
  if (e.target.classList.contains('rv-bulk-reject')) {
    const ids = Array.from(state.review.selectedIds);
    _ui1RunRejectOnTargets(ids);
    return;
  }
  if (e.target.classList.contains('rv-bulk-clear')) {
    state.review.selectedIds = new Set();
    render();
    return;
  }
}

// Bypass the prompt() and apply a pre-collected reason. Used by the
// Triage reject modal which gathers reason via reason chips + textarea.
async function _ui1RunRejectOnTargetsWithReason(ids, reason) {
  if (!ids.length || state.review.bulkInProgress) return;
  state.review.bulkInProgress = true;
  state.review.actionError = null;
  render();
  let okCount = 0;
  const errors = [];
  for (const id of ids) {
    try {
      await rejectReview(id, reason);
      okCount++;
    } catch (err) {
      errors.push({ id, message: err.message || 'reject failed' });
    }
  }
  state.review.bulkInProgress = false;
  state.review.selectedIds = new Set();
  state.review.actionError = errors.length
    ? `Rejected ${okCount}/${ids.length}; ${errors.length} failed.`
    : null;
  try {
    const result = await loadReviewItems(state.review.statusFilter);
    state.review.items = result.items;
    state.review.total = result.total;
  } catch (_e) { /* leave stale list */ }
  render();
  if (errors.length === 0) {
    tgShowToast(`Rejected ${okCount}`, 'ok');
  } else {
    tgShowToast(`Rejected ${okCount}/${ids.length}; ${errors.length} failed`, 'err');
  }
}

document.addEventListener('keydown', _ui1HandleKey);
document.addEventListener('click', _ui1HandleClick);

// Topbar ? button -> reuse the existing UI-1 shortcuts overlay.
document.addEventListener('click', function (e) {
  const btn = e.target && e.target.closest && e.target.closest('#tg-help-btn');
  if (btn) {
    e.preventDefault();
    if (typeof _ui1ToggleShortcutsOverlay === 'function') _ui1ToggleShortcutsOverlay();
    return;
  }
  // Topbar + button or floating action button -> open Quick Add.
  const addBtn = e.target && e.target.closest
    && (e.target.closest('#tg-add-btn') || e.target.closest('#tg-fab'));
  if (addBtn) {
    e.preventDefault();
    tgOpenQuickAdd();
    return;
  }
  // Due/when quick-pick chips — clicking sets datetime-local input
  // value AND state.quickAdd.dueAtLocal without re-rendering (focus
  // stays where the operator put it).
  const dueChip = e.target.closest && e.target.closest('[data-tg-qa-due-chip]');
  if (dueChip && state.quickAdd && state.quickAdd.open) {
    e.preventDefault();
    const value = dueChip.dataset.tgQaDueChip || '';
    state.quickAdd.dueAtLocal = value;
    const inp = document.getElementById('tg-qa-due');
    if (inp) inp.value = value;
    // Visual: mark active chip.
    document.querySelectorAll('.tg-qa-chip').forEach(el => el.classList.remove('on'));
    dueChip.classList.add('on');
    return;
  }

  // Quick Add modal interactions
  if (state.quickAdd && state.quickAdd.open) {
    // Dedup confirm modal — handled FIRST because it sits on top.
    const dedup = state.quickAdd.dedup;
    if (dedup) {
      if (e.target.closest('[data-tg-dedup-cancel]')
          || (e.target.matches
              && e.target.matches('[data-tg-dedup-backdrop]'))) {
        // Back to the form, keep typed values.
        state.quickAdd.dedup = null;
        render();
        return;
      }
      if (e.target.closest('[data-tg-dedup-force]')) {
        e.preventDefault();
        const previewToken = dedup.previewToken;
        state.quickAdd.dedup = null;
        const snap = _tgSnapshotQuickAddForm();
        let dueAtIso = null;
        if (snap.dueAtLocal) {
          const d = new Date(snap.dueAtLocal);
          if (!Number.isNaN(d.getTime())) dueAtIso = d.toISOString();
        }
        _tgCommitQuickAdd({ snap, dueAtIso, previewToken,
                            forceCreate: true });
        return;
      }
      const pickBtn = e.target.closest('[data-tg-dedup-pick]');
      if (pickBtn) {
        e.preventDefault();
        const taskId = pickBtn.dataset.tgDedupPick;
        // Close Quick Add and deep-link into Tasks. The PWA's Tasks
        // view supports ?task=<id> auto-expand (chunk 6).
        state.quickAdd = {
          open: false, summary: '', businessSlug: '', dueAtLocal: '',
          description: '', kind: 'task', submitting: false, error: null,
          ownerUserId: '', members: [], membersLoading: false, dedup: null,
        };
        state.view = 'tasks';
        try { window.history.replaceState(null, '',
                                          `/?task=${encodeURIComponent(taskId)}`); }
        catch (_e) {}
        render();
        if (typeof refreshTasks === 'function') {
          refreshTasks().then(() => {
            if (typeof _autoExpandDeepLinkedTask === 'function') {
              _autoExpandDeepLinkedTask(taskId).catch(() => {});
            }
          }).catch(() => {});
        }
        if (typeof tgShowToast === 'function') {
          tgShowToast('Opened existing task', 'ok');
        }
        return;
      }
      // Dedup modal swallows other clicks so the underlying form
      // can't be edited until the operator decides.
      return;
    }

    if (e.target.closest('[data-tg-qa-cancel]')
        || (e.target.matches && e.target.matches('[data-tg-qa-backdrop]'))) {
      if (state.quickAdd.submitting) return;
      state.quickAdd.open = false;
      state.quickAdd.error = null;
      render();
      return;
    }
    if (e.target.closest('[data-tg-qa-submit]')) {
      e.preventDefault();
      _tgSubmitQuickAdd();
      return;
    }
  }
});

// When the operator changes the business in Quick Add, reload the
// member list so the assignee dropdown reflects the new business.
// Codex B3-2 blocker: snapshot live form fields BEFORE we re-render,
// otherwise render() rebuilds the modal from stale state and wipes
// title/due/notes the operator already typed.
document.addEventListener('change', function (e) {
  if (e.target && e.target.id === 'tg-qa-business'
      && state.quickAdd && state.quickAdd.open) {
    _tgSnapshotQuickAddForm();   // capture title/due/notes/etc.
    const newSlug = e.target.value;
    state.quickAdd.businessSlug = newSlug;
    state.quickAdd.ownerUserId = '';   // prior pick may not be a
                                        // member of the new business.
    _tgLoadMembers(newSlug);
  }
});

// Search input on the Triage subtabs row updates state.review.searchQuery
// without re-rendering on every keystroke (debounce by re-render only
// on input event; the input keeps focus across renders since React
// does not own this DOM and the input value is bound from state).
// Live sync for Quick Add fields. Without this, an intentional
// structural Quick Add re-render (submit validation, dedup confirm)
// rebuilds the modal from stale state and wipes whatever the operator
// typed in the meantime.
// We do NOT call render() on these — typing into an input shouldn't
// cause a rerender that loses the cursor position.
const _QA_FIELD_KEYS = {
  'tg-qa-summary': 'summary',
  'tg-qa-due': 'dueAtLocal',
  'tg-qa-description': 'description',
  'tg-qa-kind': 'kind',
  'tg-qa-owner': 'ownerUserId',
};
// B4 edit-form fields. Same live-sync pattern as Quick Add.
const _EDIT_FIELD_KEYS = {
  'tg-edit-summary': 'summary',
  'tg-edit-due': 'due_at_local',
  'tg-edit-category': 'category',
  'tg-edit-dependency': 'dependency_text',
  'tg-edit-completion-note': 'completion_note',
  'tg-edit-reason': 'edit_reason',
};
document.addEventListener('input', function (e) {
  if (!e.target || !e.target.id) return;
  if (state.quickAdd && state.quickAdd.open
      && _QA_FIELD_KEYS[e.target.id]) {
    state.quickAdd[_QA_FIELD_KEYS[e.target.id]] = e.target.value;
    return;
  }
  // B4 edit form live sync.
  if (state.review.editingId && state.review.editDraft
      && _EDIT_FIELD_KEYS[e.target.id]) {
    state.review.editDraft[_EDIT_FIELD_KEYS[e.target.id]] = e.target.value;
    return;
  }
});
// 'change' fires for selects and the datetime-local picker on commit.
document.addEventListener('change', function (e) {
  if (!e.target || !e.target.id) return;
  if (state.quickAdd && state.quickAdd.open
      && _QA_FIELD_KEYS[e.target.id]) {
    state.quickAdd[_QA_FIELD_KEYS[e.target.id]] = e.target.value;
  }
  if (state.review.editingId && state.review.editDraft
      && _EDIT_FIELD_KEYS[e.target.id]) {
    state.review.editDraft[_EDIT_FIELD_KEYS[e.target.id]] = e.target.value;
  }
});

document.addEventListener('input', function (e) {
  if (e.target && e.target.id === 'tg-search-input') {
    state.review.searchQuery = e.target.value;
    state.review.focusedIndex = null;  // reset focus on new filter
    render();
    // Restore focus to the input after re-render.
    requestAnimationFrame(() => {
      const el = document.getElementById('tg-search-input');
      if (el && el !== document.activeElement) {
        el.focus();
        el.setSelectionRange(el.value.length, el.value.length);
      }
    });
  }
});

(async function () {
  // Boot with offline tolerance. /whoami and /v1/businesses fall back
  // to localStorage if they fail (Codex chunk-6-close fix). Outbox
  // hydration runs even on the offline path so optimistic patches
  // survive an airplane-mode reload.
  registerServiceWorker();
  attachConnectivityHandlers();
  try {
    state.principal = await _bootPrincipal();
    state.businesses = await _bootBusinesses();
    await hydrateOptimisticFromOutbox();
    await refreshSyncCounters();
    // Sub-(c) commit 2: deep-link auto-expand. If the URL has
    // ?task=<id>, set the Tasks view's filter to 'all' (so a
    // closed task linked from a notification doesn't get hidden
    // by the default 'open' filter), refresh tasks, and try to
    // expand the matching row. If the task isn't in the visible
    // list (e.g. user has many businesses, page-1 doesn't
    // include it), fall back to fetching it directly via
    // /v1/tasks/{id} and surfacing it as the expanded detail
    // even if the list row doesn't render.
    const deepLinkTaskId = _readDeepLinkTaskId();
    if (deepLinkTaskId) {
      state.view = 'tasks';
      state.filters.status = 'all';
      await refreshTasks();
      await _autoExpandDeepLinkedTask(deepLinkTaskId);
    } else {
      // Phase UI-2 (2026-05-09): Triage is the default landing tab
      // for admins. Owner-only principals don't see Triage in the
      // top nav, so they keep the Tasks landing.
      const isAdmin = _isPlatformAdmin(state.principal);
      if (isAdmin) {
        state.view = 'review';
        try {
          const result = await loadReviewItems(state.review.statusFilter);
          state.review.items = result.items;
          state.review.total = result.total;
          render();
        } catch (_e) { /* fall through to refreshTasks default */ }
      } else {
        await refreshTasks();
      }
    }
    replayOutbox().catch(function () {});
    setInterval(function () {
      replayOutbox().catch(function () {});
    }, 10_000);
  } catch (err) {
    // Truly first-run + offline: no persisted principal, /whoami
    // failed. Render the error UI.
    renderError(err);
  }
})();
