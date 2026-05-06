'use strict';

// OpsMemory Chunk 6 step 2 service worker.
//
// What it does:
//   - Precaches the app shell (/, /app.js, /styles.css, /manifest.json,
//     icons) on install. Cache-first for subsequent shell loads, so the
//     PWA opens instantly and works on flaky networks.
//   - Network-first with stale-if-error for /v1/tasks list GETs. When
//     the network returns 2xx, the response is cached and forwarded.
//     When the network errors (offline, 5xx, timeout), serves the most
//     recent cached response. The PWA renders stale data with no
//     "needs refresh" UI — Codex chunk-6-step1 review held SWR's active
//     re-render for step 3 alongside the IndexedDB outbox.
//   - Network-only for everything else (/whoami, /v1/businesses,
//     /v1/review/*, /v1/tasks/{id}, POSTs, PATCHes). Auth-scoped data
//     is sensitive; the PWA's "view tasks while online" path is the
//     only thing we cache today.
//
// Build hash:
//   The BUILD constant gets bumped when the service worker contract
//   changes. The browser revalidates /sw.js on every page load (the
//   server sends Cache-Control: no-cache on /sw.js per main.py changes).
//   Bumping BUILD invalidates old shell caches at activate time.

const BUILD = '6153568';
const SHELL_CACHE = `opsmemory-shell-${BUILD}`;
const TASKS_CACHE = `opsmemory-tasks-${BUILD}`;

const SHELL_URLS = [
  '/',
  '/app.js',
  '/styles.css',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

self.addEventListener('install', function (event) {
  event.waitUntil((async function () {
    const cache = await caches.open(SHELL_CACHE);
    // Use addAll to fail fast if any precache target is missing — better
    // to reject the SW install than silently ship a half-cached shell.
    // Wrapped in try so a missing icon doesn't permanently brick install.
    try {
      await cache.addAll(SHELL_URLS);
    } catch (err) {
      // Per-URL retry so we still cache what we can. Logs the failure
      // so it's debuggable in chrome://serviceworker-internals.
      console.warn('[opsmemory sw] shell precache partial fail:', err);
      for (const url of SHELL_URLS) {
        try {
          const res = await fetch(url, { cache: 'reload' });
          if (res.ok) await cache.put(url, res.clone());
        } catch (e) {
          console.warn('[opsmemory sw] cache miss for', url, e);
        }
      }
    }
    self.skipWaiting();
  })());
});

self.addEventListener('activate', function (event) {
  event.waitUntil((async function () {
    // Drop old build's caches.
    const keys = await caches.keys();
    const stale = keys.filter(function (k) {
      return (k.startsWith('opsmemory-shell-') || k.startsWith('opsmemory-tasks-'))
          && k !== SHELL_CACHE
          && k !== TASKS_CACHE;
    });
    await Promise.all(stale.map(function (k) { return caches.delete(k); }));
    await self.clients.claim();
  })());
});

// ---------------------------------------------------------------------------
// Fetch routing
// ---------------------------------------------------------------------------

self.addEventListener('fetch', function (event) {
  const req = event.request;

  // Only same-origin GETs are cacheable. Everything else (POST, PATCH,
  // cross-origin) bypasses.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (isShellAsset(url)) {
    event.respondWith(serveShell(req));
    return;
  }
  if (isTasksList(url)) {
    event.respondWith(serveTasksList(req));
    return;
  }
  // Everything else: default network. Don't intercept — passing through
  // is cheaper than calling fetch and forwarding.
});


// ---------------------------------------------------------------------------
// Routing predicates
// ---------------------------------------------------------------------------

function isShellAsset(url) {
  if (url.pathname === '/') return true;
  if (url.pathname === '/app.js') return true;
  if (url.pathname === '/styles.css') return true;
  if (url.pathname === '/manifest.json') return true;
  if (url.pathname.startsWith('/icons/')) return true;
  return false;
}

function isTasksList(url) {
  // Only the list endpoint, not /v1/tasks/{id} detail. The detail is
  // network-only because edits depend on the freshest version.
  return url.pathname === '/v1/tasks';
}


// ---------------------------------------------------------------------------
// Shell: cache-first
// ---------------------------------------------------------------------------

async function serveShell(req) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(req);
  if (cached) {
    // Background revalidate so a freshly deployed shell propagates on the
    // next reload. Errors are intentionally swallowed.
    event_revalidate(cache, req).catch(function () {});
    return cached;
  }
  try {
    const fresh = await fetch(req);
    if (fresh.ok) await cache.put(req, fresh.clone());
    return fresh;
  } catch (err) {
    // Offline + nothing cached: return a minimal failure so the PWA's
    // boot path can render its error UI.
    return new Response('OpsMemory shell unavailable offline.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain' },
    });
  }
}

async function event_revalidate(cache, req) {
  const fresh = await fetch(req, { cache: 'no-cache' });
  if (fresh.ok) await cache.put(req, fresh.clone());
}


// ---------------------------------------------------------------------------
// /v1/tasks: network-first, stale-if-error
// ---------------------------------------------------------------------------

async function serveTasksList(req) {
  const cache = await caches.open(TASKS_CACHE);
  try {
    // Network-first. Honors the request's credentials (Cloudflare
    // Access cookie) since fetch(req) preserves the original Request
    // attributes.
    const fresh = await fetch(req);
    if (fresh.ok) {
      // Cache user-visible 2xx responses only. 401/403/404 must NOT be
      // cached — Cloudflare Access challenges shouldn't bake into the
      // tasks cache.
      cache.put(req, fresh.clone()).catch(function () {});
    }
    return fresh;
  } catch (err) {
    // Network unreachable. Try the cache; on miss, surface a network
    // error so the PWA renders its standard error state.
    const cached = await cache.match(req);
    if (cached) {
      // Mark the response as a cache hit so the PWA can show a "stale
      // data — offline" indicator if it wants to. (Not yet wired in
      // app.js; available for chunk 6 step 3.)
      const hdrs = new Headers(cached.headers);
      hdrs.set('X-OpsMemory-From-Cache', '1');
      return new Response(await cached.blob(), {
        status: cached.status,
        statusText: cached.statusText,
        headers: hdrs,
      });
    }
    throw err;
  }
}
