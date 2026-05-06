'use strict';

// OpsMemory Chunk 6 step 2 service worker.
//
// What it does:
//   - Precaches the app shell (/, /app.js, /styles.css, /manifest.json,
//     icons) on install. Cache-first for subsequent shell loads, so the
//     PWA opens instantly and works on flaky networks.
//   - Network-first with stale-if-error for /v1/tasks list GETs. When
//     the network returns 2xx, the response is cached (await — durable
//     before responding) and forwarded. When the network throws (offline,
//     timeout) OR returns 5xx, falls back to the most recent cached
//     response with X-OpsMemory-From-Cache: 1. 401/403/404 are returned
//     unchanged (auth failures and not-founds must NOT serve stale —
//     they reflect real state changes). The PWA renders stale data with
//     no "needs refresh" UI — Codex chunk-6-step1 review held SWR's
//     active re-render for step 3 alongside the IndexedDB outbox.
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

const BUILD = 'c10s3a-fix-loading-weekday';
const SHELL_CACHE = `opsmemory-shell-${BUILD}`;
const TASKS_CACHE = `opsmemory-tasks-${BUILD}`;

// Required: install fails if any of these don't precache. Loss of these
// breaks the offline boot path entirely.
const SHELL_URLS_REQUIRED = [
  '/',
  '/app.js',
  '/outbox.js',
  '/styles.css',
  '/manifest.json',
];

// Optional: install tolerates failures here. A missing icon doesn't
// brick the app; on-demand serve in serveShell() catches misses.
const SHELL_URLS_OPTIONAL = [
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-512-maskable.png',
];

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

self.addEventListener('install', function (event) {
  event.waitUntil((async function () {
    const cache = await caches.open(SHELL_CACHE);
    // Required core: addAll throws on any miss -> install rejects, the
    // old SW (if any) keeps serving. We do NOT silently degrade to a
    // half-cached shell because that would brick offline boot.
    await cache.addAll(SHELL_URLS_REQUIRED);
    // Optional icons: per-URL fetch with swallow. A missing maskable
    // icon shouldn't reject install.
    for (const url of SHELL_URLS_OPTIONAL) {
      try {
        const res = await fetch(url, { cache: 'reload' });
        if (res.ok) await cache.put(url, res.clone());
      } catch (e) {
        console.warn('[opsmemory sw] optional asset cache miss for', url, e);
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
  if (url.pathname === '/outbox.js') return true;
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
    // Network-first. fetch(req) preserves credentials so the
    // Cloudflare Access cookie rides along.
    const fresh = await fetch(req);
    if (fresh.ok) {
      // AWAIT the cache write so the SW isn't terminated before it
      // completes (Codex chunk-6-step2 blocker — fire-and-forget put
      // could lose the cache entry). Clone first; .clone() is cheap
      // because the body is just buffered, not re-fetched.
      try {
        await cache.put(req, fresh.clone());
      } catch (e) {
        // QuotaExceededError or similar — log and serve fresh
        // anyway. We'd rather respond now than fail the user.
        console.warn('[opsmemory sw] tasks cache.put failed:', e);
      }
      return fresh;
    }
    // 5xx: try stale cache before propagating the server failure
    // (Codex chunk-6-step2 blocker — original code only fell back on
    // throw, so 502 from CF would surface unmodified).
    if (fresh.status >= 500 && fresh.status <= 599) {
      const stale = await _staleResponse(cache, req);
      if (stale) return stale;
    }
    // 401/403/404 and other 4xx: return as-is. Auth failures and
    // not-founds must NOT be served stale — they reflect real state
    // changes (Cloudflare Access lockout, deleted task).
    return fresh;
  } catch (err) {
    // Network unreachable. Stale cache, else error.
    const stale = await _staleResponse(cache, req);
    if (stale) return stale;
    throw err;
  }
}

async function _staleResponse(cache, req) {
  const cached = await cache.match(req);
  if (!cached) return null;
  // Mark the response as a cache hit so app.js (step 3) can show a
  // "stale — offline" indicator. Same-origin response, controlled by
  // us, no CORS leak risk.
  const hdrs = new Headers(cached.headers);
  hdrs.set('X-OpsMemory-From-Cache', '1');
  return new Response(await cached.blob(), {
    status: cached.status,
    statusText: cached.statusText,
    headers: hdrs,
  });
}
