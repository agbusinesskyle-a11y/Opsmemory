'use strict';

// Chunk 1 service worker — bare minimum.
// Pass-through fetch handler. No caching, no offline outbox.
// Caching + outbox land in Chunk 6.

self.addEventListener('install', function () {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function () {
  // Pass-through. Browser handles the request directly.
});
