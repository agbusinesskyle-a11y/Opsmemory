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
  view: 'tasks',                // 'tasks' | 'review' (review admin-only)
  review: {
    items: [],
    total: 0,
    statusFilter: 'pending,needs_changes',
    expandedId: null,
    expandedDetail: null,
    pendingAction: null,        // 'approve' | 'reject' | null while a write is in flight
    actionError: null,
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
  if (res.status === 401) throw { kind: 'auth', message: 'Session expired. Refresh the page.' };
  if (res.status === 403) throw { kind: 'forbidden', message: 'You don\'t have permission for that.' };
  if (res.status === 404) throw { kind: 'not_found', message: 'Not found.', body };
  if (res.status === 409) throw { kind: 'conflict', message: 'Conflict — the underlying task changed. Re-check and retry.', body };
  if (res.status === 422) throw { kind: 'validation', message: 'Validation failed.', body };
  if (res.status === 501) throw { kind: 'not_implemented', message: 'Not implemented yet.', body };
  if (!res.ok) throw { kind: 'server', message: `Server returned ${res.status}.`, body };
  return body;
}

async function loadPrincipal() {
  return await api('/whoami');
}

async function loadBusinesses() {
  const data = await api('/v1/businesses');
  return data.businesses || [];
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
  const expanded = state.expandedTaskId === task.id;
  const detail = expanded ? state.expandedTaskDetail : null;

  const dueLine = task.due_at
    ? `<div class="task-due">due ${escapeHtml(fmtDate(task.due_at))}</div>`
    : '';
  const depLine = task.dependency_text
    ? `<div class="task-dep">⏸ waiting on ${escapeHtml(task.dependency_text)}</div>`
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
      </div>
    `;
  }

  return `
    <li class="task ${task.status}${expanded ? ' expanded' : ''}" data-task-id="${escapeHtml(task.id)}">
      <div class="task-header">
        <div class="task-summary">${escapeHtml(task.summary)}</div>
        <div class="task-status-pill ${task.status}">${escapeHtml(task.status)}</div>
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

function render() {
  const root = document.getElementById('root');
  if (!root) return;
  if (state.view === 'review') {
    root.innerHTML = `
      ${renderHeader()}
      <div id="review-area">${renderReviewList()}</div>
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
      } else {
        render();
      }
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
      // Don't toggle when clicking on inner links (none yet, but safe)
      if (e.target.closest('a')) return;
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
}

async function refreshTasks() {
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

(async function () {
  try {
    state.principal = await loadPrincipal();
    state.businesses = await loadBusinesses();
    await refreshTasks();
    registerServiceWorker();
  } catch (err) {
    renderError(err);
    registerServiceWorker();
  }
})();
