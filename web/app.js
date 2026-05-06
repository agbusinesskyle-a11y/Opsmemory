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
  view: 'tasks',                // 'tasks' | 'review' | 'sops' (review/sops admin-only)
  review: {
    items: [],
    total: 0,
    statusFilter: 'pending,needs_changes',
    expandedId: null,
    expandedDetail: null,
    pendingAction: null,        // 'approve' | 'reject' | null while a write is in flight
    actionError: null,
  },
  // SOPs view (chunk 7 step 4 first UI commit — read-only).
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
  const tabs = `
    <div class="view-tabs">
      <button class="view-tab${state.view === 'tasks' ? ' active' : ''}" data-view="tasks">Tasks</button>
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

function renderSopVersionRow(version, expanded) {
  const stateClass = (version.state || 'draft').replace('_', '-');
  const publishedLine = version.published_at
    ? `<span class="muted">published ${escapeHtml(fmtRelative(version.published_at))}</span>`
    : '';
  let templatesBlock = '';
  if (expanded) {
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
  return `
    <li class="sop-version ${stateClass}${expanded ? ' expanded' : ''}" data-version-no="${escapeHtml(String(version.version_no))}">
      <div class="sop-version-head">
        <span class="version-no">v${escapeHtml(String(version.version_no))}</span>
        <span class="state-pill ${stateClass}">${escapeHtml(version.state)}</span>
        ${publishedLine}
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
      detailBlock = `
        <div class="sop-detail">
          ${detail.sop.description ? `<div class="sop-description">${escapeHtml(detail.sop.description)}</div>` : ''}
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
      <div id="sops-area">${renderSopList()}</div>
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
// Event handlers
// ---------------------------------------------------------------------------

function attachEventHandlers() {
  // ----- view tabs -----
  document.querySelectorAll('.view-tab').forEach(btn => {
    btn.addEventListener('click', async () => {
      const v = btn.dataset.view;
      if (state.view === v) return;
      state.view = v;
      state.review.actionError = null;
      if (v === 'review') {
        await refreshReviewItems();
      } else if (v === 'sops') {
        state.sops.loadError = null;
        await refreshSops();
      } else {
        render();
      }
    });
  });

  // ----- SOPs view handlers -----
  document.querySelectorAll('.sop-status-tab').forEach(btn => {
    btn.addEventListener('click', async () => {
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
      if (state.sops.expandedId === sopId) {
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
  document.querySelectorAll('.sop-version').forEach(li => {
    li.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (e.target.closest('button')) return;
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
