'use strict';

// Chunk 1 PWA shell. Calls /whoami, renders principal + businesses, registers SW.
// Real task list arrives in Chunk 2.

async function loadWhoami() {
  try {
    const res = await fetch('/whoami', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });

    if (res.status === 401) {
      return { error: 'unauthenticated', message: 'Session expired. Refresh the page to sign in again.' };
    }
    if (res.status === 403) {
      return { error: 'forbidden', message: 'This account is not enabled for OpsMemory. Contact Kyle to be added.' };
    }
    if (!res.ok) {
      return { error: 'server_error', message: `Server returned ${res.status}.` };
    }
    return await res.json();
  } catch (err) {
    return { error: 'network', message: 'Could not reach the server. Check your connection and try again.' };
  }
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, function (c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}

function render(state) {
  const root = document.getElementById('root');
  if (!root) return;

  if (state.error) {
    root.innerHTML = '<div class="error">' + escapeHtml(state.message) + '</div>';
    return;
  }

  const businesses = (state.businesses || [])
    .map(function (b) {
      return '<li><span>' + escapeHtml(b.name) + '</span>' +
             '<span class="role-pill">' + escapeHtml(b.role) + '</span></li>';
    })
    .join('');

  root.innerHTML =
    '<div class="principal">' +
      'Logged in as <strong>' + escapeHtml(state.display_name) + '</strong>' +
      '<span class="role-pill">' + escapeHtml(state.role) + '</span>' +
      '<div class="email">' + escapeHtml(state.email || '') + '</div>' +
    '</div>' +
    '<h2>Your businesses</h2>' +
    '<ul class="businesses">' + (businesses || '<li>(none)</li>') + '</ul>' +
    '<div class="empty-state">' +
      'No open tasks yet. Task list arrives in Chunk 2.' +
    '</div>' +
    '<div class="footer-note">' +
      'OpsMemory — chunk 1 substrate' +
    '</div>';
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register('/sw.js', { scope: '/' });
  } catch (err) {
    console.warn('SW registration failed:', err);
  }
}

(async function () {
  const state = await loadWhoami();
  render(state);
  registerServiceWorker();
})();
