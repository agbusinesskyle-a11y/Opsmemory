'use strict';

// OpsMemory PWA outbox — IndexedDB persistence for offline-safe writes.
//
// Schema (per Codex chunk-6-step2 STEP 3 PLAN):
//   object store 'mutations', keyPath 'idempotency_key'
//   indexes: status, created_at, task_id
//   row shape:
//     idempotency_key       uuid
//     principal_id          str (the user who enqueued)
//     action                'toggle_done' | future actions
//     method                'POST' | 'PATCH' | ...
//     path                  '/v1/tasks/{id}/toggle_done' | ...
//     task_id               uuid (foreign key to the affected task)
//     body                  the request body the server expects
//     base_task_version     int (echoed from body for index/UI)
//     base_field_versions   {field: int}
//     previous_task_snapshot  the last-known task state (for 409 revert)
//     optimistic_patch      {field: value} we applied to UI immediately
//     created_at            ISO timestamp
//     updated_at            ISO timestamp
//     attempt_count         int
//     next_attempt_at       ISO timestamp (for backoff)
//     status                'pending' | 'applied' | 'conflict' | 'rejected' | 'discarded'
//     last_status           int (HTTP status of last attempt)
//     last_error            string (server error code or local exception)
//     server_payload        raw response body of last attempt

const DB_NAME = 'opsmemory';
const DB_VERSION = 1;
const STORE = 'mutations';

let _dbPromise = null;

function openDB() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise(function (resolve, reject) {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = function (e) {
      const db = e.target.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const os = db.createObjectStore(STORE, { keyPath: 'idempotency_key' });
        os.createIndex('status', 'status', { unique: false });
        os.createIndex('created_at', 'created_at', { unique: false });
        os.createIndex('task_id', 'task_id', { unique: false });
      }
    };
    req.onsuccess = function () { resolve(req.result); };
    req.onerror = function () { reject(req.error); };
    req.onblocked = function () {
      console.warn('[opsmemory outbox] IDB open blocked by another tab');
    };
  });
  return _dbPromise;
}

async function _txReadwrite() {
  const db = await openDB();
  return db.transaction([STORE], 'readwrite').objectStore(STORE);
}

async function _txReadonly() {
  const db = await openDB();
  return db.transaction([STORE], 'readonly').objectStore(STORE);
}

function _wrap(req) {
  return new Promise(function (resolve, reject) {
    req.onsuccess = function () { resolve(req.result); };
    req.onerror = function () { reject(req.error); };
  });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

async function enqueue(mutation) {
  const now = new Date().toISOString();
  const row = Object.assign({
    created_at: now,
    updated_at: now,
    attempt_count: 0,
    next_attempt_at: now,
    status: 'pending',
    last_status: null,
    last_error: null,
    server_payload: null,
  }, mutation);
  const os = await _txReadwrite();
  await _wrap(os.put(row));
  return row;
}

async function getByKey(idempotency_key) {
  const os = await _txReadonly();
  return await _wrap(os.get(idempotency_key));
}

async function getByStatus(status_value) {
  const os = await _txReadonly();
  const idx = os.index('status');
  return await _wrap(idx.getAll(status_value));
}

async function getByTask(task_id) {
  const os = await _txReadonly();
  const idx = os.index('task_id');
  return await _wrap(idx.getAll(task_id));
}

async function update(idempotency_key, patch) {
  const os = await _txReadwrite();
  const existing = await _wrap(os.get(idempotency_key));
  if (!existing) return null;
  const updated = Object.assign({}, existing, patch, {
    updated_at: new Date().toISOString(),
  });
  await _wrap(os.put(updated));
  return updated;
}

async function discard(idempotency_key) {
  return await update(idempotency_key, {
    status: 'discarded',
    last_error: 'user_discarded',
  });
}

async function purgeApplied(maxAgeMs) {
  // Drop applied/discarded rows older than maxAgeMs to keep the
  // outbox small. Defaults to 7 days.
  const cutoff = new Date(Date.now() - (maxAgeMs || 7 * 24 * 60 * 60 * 1000)).toISOString();
  const os = await _txReadwrite();
  const idx = os.index('status');
  let removed = 0;
  for (const status_value of ['applied', 'discarded']) {
    const rows = await _wrap(idx.getAll(status_value));
    for (const r of rows) {
      if (r.updated_at < cutoff) {
        await _wrap(os.delete(r.idempotency_key));
        removed++;
      }
    }
  }
  return removed;
}

function newKey() {
  // crypto.randomUUID is widely available on modern PWAs (Chrome 92+,
  // Safari 15.4+, Firefox 95+). Fall back to a simple random hex if
  // not — collisions vanishingly unlikely for our scale.
  if (self.crypto && self.crypto.randomUUID) {
    return self.crypto.randomUUID();
  }
  const a = new Uint8Array(16);
  self.crypto.getRandomValues(a);
  let s = '';
  for (let i = 0; i < a.length; i++) {
    s += a[i].toString(16).padStart(2, '0');
  }
  return s.slice(0, 8) + '-' + s.slice(8, 12) + '-4' + s.slice(13, 16) +
         '-' + s.slice(16, 20) + '-' + s.slice(20, 32);
}

// Expose as a single global so app.js can use it without a module
// import (current PWA loads scripts via plain <script src=...>).
self.OpsMemoryOutbox = {
  openDB,
  enqueue,
  getByKey,
  getByStatus,
  getByTask,
  update,
  discard,
  purgeApplied,
  newKey,
};
