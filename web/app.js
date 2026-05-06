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
  params.set('limit', '50');
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
  const isAdmin = p.role === 'admin';
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
        ? `<button class="view-tab${state.view === 'review' ? ' active' : ''}" data-view="review">Review</button>`
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

function renderReviewItem(item) {
  const expanded = state.review.expandedId === item.id;
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
  return `
    <li class="rv-item ${statusClass}${expanded ? ' expanded' : ''}" data-review-id="${escapeHtml(item.id)}">
      <div class="rv-item-header">
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
  return `
    <div class="task-count">${state.review.total} review item${state.review.total === 1 ? '' : 's'}</div>
    <ul class="rv-list">${state.review.items.map(renderReviewItem).join('')}</ul>
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

function render() {
  const root = document.getElementById('root');
  if (!root) return;
  if (state.view === 'review') {
    root.innerHTML = `
      ${renderHeader()}
      <div id="review-area">${renderReviewList()}</div>
    `;
  } else if (state.view === 'sops') {
    root.innerHTML = `
      ${renderHeader()}
      ${renderSopFilters()}
      ${renderCreateSopForm()}
      <div id="sops-area">${renderSopList()}</div>
      ${renderAnchorsSection()}
    `;
  } else if (state.view === 'settings') {
    root.innerHTML = `
      ${renderHeader()}
      <div id="settings-area">${renderSettings()}</div>
    `;
  } else {
    root.innerHTML = `
      ${renderHeader()}
      ${renderFilters()}
      <div id="task-area">${renderTaskList()}</div>
    `;
  }
  attachEventHandlers();
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
  const subRows = (s.subscriptions || []).map(sub => {
    const pending = !!revokes[sub.id];
    return `
    <tr>
      <td>${escapeHtml(sub.device_label || '(unlabeled device)')}</td>
      <td><code class="settings-keyfrag" title="${escapeHtml(sub.endpoint || '')}">${escapeHtml(_shortenKey(sub.endpoint))}</code></td>
      <td><span class="settings-pill badge-${sub.status === 'active' ? 'granted' : 'default'}">${escapeHtml(sub.status)}</span></td>
      <td class="muted">${escapeHtml(sub.last_seen_at || sub.created_at || '')}</td>
      <td class="settings-actions-cell">
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

  return `
    <section class="settings-section">
      <h3>Web Push</h3>
      ${_renderSettingsLine('Browser Push API', '<span class="settings-pill badge-granted">supported</span>')}
      ${_renderSettingsLine('Notification permission', _renderPermissionPill(s.permissionState))}
      ${_renderSettingsLine('Service worker', swStatus)}
      ${_renderSettingsLine('Server VAPID key', vapidStatus)}
      ${_renderSettingsLine('This browser', browserSubStatus)}
      <h4>Active subscriptions</h4>
      ${revokeErrorBlock}
      ${subsTable}
      <p class="muted">Enable controls land in the next update.</p>
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
      // Re-check after confirm (user may have clicked Revoke
      // twice while the confirm dialog was open).
      if (revokes[subId]) return;

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
    await refreshTasks();
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
