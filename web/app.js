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
};

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------

async function api(path) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' },
  });
  if (res.status === 401) throw { kind: 'auth', message: 'Session expired. Refresh the page.' };
  if (res.status === 403) throw { kind: 'forbidden', message: 'You don\'t have permission for that.' };
  if (res.status === 404) throw { kind: 'not_found', message: 'Not found.' };
  if (!res.ok) throw { kind: 'server', message: `Server returned ${res.status}.` };
  return await res.json();
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
  return `
    <div class="principal">
      <strong>${escapeHtml(p.display_name)}</strong>
      <span class="role-pill">${escapeHtml(p.role)}</span>
      <div class="email">${escapeHtml(p.email || '')}</div>
      <div class="biz-list">${businesses}</div>
    </div>
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

function render() {
  const root = document.getElementById('root');
  if (!root) return;
  root.innerHTML = `
    ${renderHeader()}
    ${renderFilters()}
    <div id="task-area">${renderTaskList()}</div>
  `;
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
